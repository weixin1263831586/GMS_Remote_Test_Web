// Notification center and browser notification integration.

const VALID_LEVELS = ['success', 'warning', 'error', 'info'];
let notificationPanelEscHandler = null;

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
    // Sync to backend so it survives panel reloads
    if (!options.skipSync) {
        try {
            apiCall('/api/notifications', 'POST', {
                title: item.title,
                message: item.message,
                level: item.level,
                category: item.category,
                data: { ...item.data, _synced_id: item.id, _synced_read: item.read }
            });
        } catch (error) {
            debugLog('[Notification] Sync to backend failed:', error);
        }
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
        const serverRecords = (payload.records || []).map(normalizeNotification);

        // Merge: keep local-only notifications that the server doesn't have
        const serverIds = new Set(serverRecords.map(r => r.id));
        const localOnly = state.notifications.filter(item => !serverIds.has(item.id));
        const mergedMap = new Map();

        // Add server records first (authoritative)
        for (const record of serverRecords) {
            mergedMap.set(record.id, record);
        }
        // Add local-only records that server doesn't have
        for (const record of localOnly) {
            mergedMap.set(record.id, record);
        }

        state.notifications = Array.from(mergedMap.values());
        state.unreadNotifications = state.notifications.filter(item => !item.read).length;
        renderNotificationList();
    } catch (error) {
        debugLog('[Notification] Load failed:', error);
    }
}

function toggleNotificationPanel() {
    const panel = $('notification-panel');
    if (!panel) return;
    if (panel.classList.contains('show')) {
        closeNotificationPanel();
        return;
    }
    panel.classList.add('show');
    loadNotifications();
    if (notificationPanelEscHandler) {
        document.removeEventListener('keydown', notificationPanelEscHandler);
    }
    notificationPanelEscHandler = (e) => {
        if (e.key === 'Escape') {
            closeNotificationPanel();
        }
    };
    document.addEventListener('keydown', notificationPanelEscHandler);
}

function closeNotificationPanel() {
    const panel = $('notification-panel');
    if (panel) panel.classList.remove('show');
    if (notificationPanelEscHandler) {
        document.removeEventListener('keydown', notificationPanelEscHandler);
        notificationPanelEscHandler = null;
    }
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
        // skipSync: already POSTed to backend above
        handleRealtimeNotification(notification || { title, message, level, category, data }, { skipSync: true });
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
