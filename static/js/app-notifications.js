// Notification center and browser notification integration.

const VALID_LEVELS = ['success', 'warning', 'error', 'info'];

function normalizeNotification(notification) {
    const now = new Date().toISOString();
    return {
        id: notification?.id || `local-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        timestamp: notification?.timestamp || now,
        title: notification?.title || '通知',
        message: notification?.message || '',
        level: VALID_LEVELS.includes(notification?.level) ? notification.level : 'info',
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

function shouldShowBrowserNotification(force = false) {
    return 'Notification' in window &&
        Notification.permission === 'granted' &&
        (force || document.visibilityState !== 'visible');
}

function showBrowserNotification(notification, force = false) {
    if (!shouldShowBrowserNotification(force)) return;
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
        showBrowserNotification(item, options.forceBrowser === true);
    }
}

function notifyOperationResult(title, message, level = 'info', category = 'system', data = {}) {
    handleRealtimeNotification(
        { title, message, level, category, data },
        { toast: false, browser: true, forceBrowser: true }
    );
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

window.toggleNotificationPanel = toggleNotificationPanel;
window.closeNotificationPanel = closeNotificationPanel;
window.requestBrowserNotificationPermission = requestBrowserNotificationPermission;
window.markNotificationRead = markNotificationRead;
window.markAllNotificationsRead = markAllNotificationsRead;
window.clearNotifications = clearNotifications;
window.renderNotificationList = renderNotificationList;
window.handleRealtimeNotification = handleRealtimeNotification;
window.notifyOperationResult = notifyOperationResult;
window.createLocalNotification = createLocalNotification;
