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
        list.innerHTML = '<div class="notification-empty ui-empty-state">暂无通知</div>';
        updateNotificationBadge();
        return;
    }

    list.innerHTML = state.notifications.map(item => `
        <div class="notification-item ${escapeHtml(item.level)} ${item.read ? '' : 'unread'}"
             data-notification-id="${escapeHtml(item.id)}"
             data-click="openNotification" data-a0="${escapeHtml(item.id)}"
             role="button" tabindex="0" aria-label="查看通知：${escapeHtml(item.title)}">
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
            void openNotification(notification.id);
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
        // apiCall 返回 Promise：必须显式 .catch，否则 rejection 不会被
        // 外层 try/catch 捕获（unhandled rejection）。
        apiCall('/api/notifications', 'POST', {
            title: item.title,
            message: item.message,
            level: item.level,
            category: item.category,
            data: { ...item.data, _synced_id: item.id, _synced_read: item.read }
        }).catch(error => {
            debugLog('[Notification] Sync to backend failed:', error);
        });
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
    $('notification-dismiss-layer')?.classList.add('show');
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
    $('notification-dismiss-layer')?.classList.remove('show');
    if (notificationPanelEscHandler) {
        document.removeEventListener('keydown', notificationPanelEscHandler);
        notificationPanelEscHandler = null;
    }
}

function dismissNotificationPanelFromPointer(event) {
    const panel = $('notification-panel');
    if (!panel?.classList.contains('show')) return;
    // The bell owns explicit toggling, while every interaction inside the
    // notification surface must remain usable without dismissing the panel.
    // Only pointer events outside both regions close it.
    if (event.target?.closest?.('#notification-toggle, #notification-panel')) return;
    closeNotificationPanel();
}

const notificationToggle = $('notification-toggle');
if (notificationToggle) {
    notificationToggle.addEventListener('click', toggleNotificationPanel);
    notificationToggle.disabled = false;
}
document.addEventListener('click', dismissNotificationPanelFromPointer);
document.addEventListener('auxclick', dismissNotificationPanelFromPointer);
document.addEventListener('contextmenu', dismissNotificationPanelFromPointer);

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

// Navigation relies on immutable IDs from the notification payload, never
// on localized titles or user-supplied free-form URLs.
function notificationDestination(item) {
    if (!item) return null;
    const data = item.data && typeof item.data === 'object' ? item.data : {};
    const category = String(item.category || '').toLowerCase();
    const text = key => typeof data[key] === 'string' ? data[key].trim() : '';
    const jobId = text('cluster_job_id') || text('job_id');
    const reportId = text('report_id');
    // A generic event timestamp is not a report timestamp and must not
    // redirect unrelated notifications to report management.
    const timestamp = text('report_timestamp')
        || ((category === 'report' || category === 'reports') ? text('timestamp') : '');
    const attemptId = text('attempt_id');
    const workerId = text('worker_id');
    const automationRunId = text('automation_run_id') || (
        category === 'automation' ? text('run_id') : ''
    );
    const taskId = text('task_id');
    const issues = text('redmine_issue_id') || text('issue_id');
    const anchor = {
        report_id: reportId, report_timestamp: timestamp,
        cluster_job_id: jobId, attempt_id: attemptId,
        automation_run_id: automationRunId, worker_id: workerId
    };

    const isTestCompletion = category === 'cluster'
        && text('command_type') === 'start_test'
        && text('status') === 'completed';
    if (reportId || timestamp || isTestCompletion
        || ((category === 'report' || category === 'reports') && jobId)) {
        return {kind: 'report', anchor};
    }
    if (category === 'report-analysis') return {kind: 'page', page: 'report-analysis'};
    if (category === 'report-download') return {kind: 'page', page: 'reports'};
    if (automationRunId) return {kind: 'workspace', page: 'automation', context: {
        automation_run_id: automationRunId, worker_id: workerId,
        scope_mode: 'cluster'
    }};
    if (category === 'apk' && taskId) return {kind: 'apk', taskId};
    if (jobId) return {kind: 'workspace', page: 'cluster', context: {
        scope_mode: 'cluster', worker_id: workerId, cluster_job_id: jobId
    }};
    if (issues) return {kind: 'workspace', page: 'redmine-agent', context: {
        redmine_issue_id: issues
    }};
    if (category === 'cluster' && workerId) return {
        kind: 'workspace', page: 'cluster', context: {
            scope_mode: 'cluster', worker_id: workerId, cluster_job_id: ''
        }
    };
    if (['suite-download', 'suite-extract', 'suite', 'report-copy'].includes(category)) {
        return {kind: 'workspace', page: 'test-suites', context: {
            // A downloaded ZIP is not a deployed suite tools_path. Do not
            // write archive_path into the global test execution selection.
            worker_id: workerId
        }};
    }
    if (['firmware', 'gsi-burn', 'device', 'devices'].includes(category)) {
        const devices = Array.isArray(data.devices)
            ? data.devices.filter(value => typeof value === 'string').slice(0, 16)
            : [];
        return {kind: 'workspace', page: 'devices', context: {
            worker_id: workerId, device_ids: devices
        }};
    }
    if (category === 'redmine') return {kind: 'page', page: 'redmine-agent'};
    return null;
}

async function navigateFromNotification(destination) {
    if (!destination) return false;
    if (destination.kind === 'report') {
        if (typeof window.focusReportFromNotification !== 'function') {
            throw new Error('报告定位模块尚未加载');
        }
        const found = await window.focusReportFromNotification(destination.anchor);
        if (found) {
            switchPage('reports', null);
            document.querySelector('#reports-table-body tr.notification-report-target')
                ?.scrollIntoView({block: 'nearest', inline: 'nearest'});
            return true;
        }
        const jobId = destination.anchor.cluster_job_id;
        if (jobId) {
            // A finished test may not have an indexed report yet. Open its
            // *own* Job diagnostics, never an unrelated report row.
            showToast('该任务的报告尚未找到，已打开对应任务详情', 'warning');
            window.GmsWorkspace?.navigate('cluster', {
                scope_mode: 'cluster', worker_id: destination.anchor.worker_id,
                cluster_job_id: jobId
            });
            return true;
        }
        showToast('对应报告不存在或当前账号无权查看', 'warning');
        return false;
    }
    if (destination.kind === 'apk') {
        switchPage('apk-analysis', null);
        if (typeof window.loadApkTaskHistory !== 'function'
            || typeof restoreApkTask !== 'function') {
            throw new Error('APK 分析模块尚未加载');
        }
        const tasks = await window.loadApkTaskHistory(false);
        if (!tasks.some(task => task.task_id === destination.taskId)) {
            showToast('对应 APK 任务不存在或已清理', 'warning');
            return false;
        }
        await restoreApkTask(destination.taskId);
        return true;
    }
    if (destination.kind === 'workspace') {
        if (window.GmsWorkspace?.navigate) {
            window.GmsWorkspace.navigate(destination.page, destination.context);
        } else {
            switchPage(destination.page, null);
        }
        return true;
    }
    if (destination.kind === 'page') {
        switchPage(destination.page, null);
        return true;
    }
    return false;
}

async function openNotification(id) {
    const item = state.notifications.find(record => record.id === id);
    if (!item) return;
    // Do not block navigation on the read acknowledgment.
    void markNotificationRead(id);
    const target = notificationDestination(item);
    if (!target) {
        showToast('通知已读；该历史消息没有可定位的业务对象', 'info');
        return;
    }
    closeNotificationPanel();
    try {
        await navigateFromNotification(target);
    } catch (error) {
        debugLog('[Notification] Navigation failed:', error);
        showToast('通知定位失败：' + (error?.message || '请稍后重试'), 'warning');
    }
}

document.getElementById('notification-list')?.addEventListener('keydown', event => {
    if (!['Enter', ' '].includes(event.key)) return;
    const item = event.target.closest?.('.notification-item[data-notification-id]');
    if (!item) return;
    event.preventDefault();
    void openNotification(item.dataset.notificationId);
});

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
window.openNotification = openNotification;
window.markAllNotificationsRead = markAllNotificationsRead;
window.clearNotifications = clearNotifications;
window.renderNotificationList = renderNotificationList;
window.handleRealtimeNotification = handleRealtimeNotification;
window.notifyOperationResult = notifyOperationResult;
window.createLocalNotification = createLocalNotification;
