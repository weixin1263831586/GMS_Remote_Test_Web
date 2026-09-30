// 防抖版本的刷新函数（force=true 时绕过缓存强制刷新）
const debouncedRefreshDevices = debounce((force = false) => loadDevices(Boolean(force)), 500);

function renderDevices() {
    debugLog('[renderDevices] Called, state.devices:', state.devices);
    const leftContainer = document.getElementById('device-list-left');
    const rightContainer = document.getElementById('device-list-right');
    const deviceCanvas = document.getElementById('device-canvas');

    debugLog('[renderDevices] Containers:', { leftContainer: !!leftContainer, rightContainer: !!rightContainer, deviceCanvas: !!deviceCanvas });

    if (!leftContainer || !rightContainer || !deviceCanvas) {
        console.warn('[renderDevices] Early return: containers not ready');
        return;
    }

    // 设备数据签名：数据未变化时跳过整表重建。手动刷新是双次渲染
    // （缓存渲染 + worker refresh 回包渲染），叠加 devices_changed /
    // catch-up 补刷并发触发，同数据重复 innerHTML 重建会造成闪屏。
    const renderSignature = JSON.stringify([
        state.devices, state.selectedDevices, state.deviceGroups,
        state.followFilter, window.GmsWorkspace?.get?.().device_ids || []
    ]);
    if (renderSignature === leftContainer.dataset.renderSignature) {
        debugLog('[renderDevices] Skip: device data unchanged');
        syncLocalUsbActionButtons();
        return;
    }
    leftContainer.dataset.renderSignature = renderSignature;

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

    const deviceFragment = document.createDocumentFragment();
    deviceInfos.forEach(deviceInfo => {
        deviceFragment.appendChild(buildDeviceItemEl(deviceInfo));
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
    // 不可选（占用/状态异常）设备保留在选中集合中，但复选框不显示勾选：
    // 复选框随不可选被禁用，设备恢复可选后勾选自动恢复。
    const isSelected = selectable && state.selectedDevices.has(deviceId);
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
        ? 'Fastboot/Fastbootd 设备可用于 GSI 烧写和重启'
        : status === 'loader'
        ? 'Loader/MaskROM 烧写模式设备，可直接选中重新烧写固件'
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
    const statusLower = String(status || '').toLowerCase();
    const displayStatus = statusLower === 'fastboot'
        ? 'Fastboot'
        : statusLower === 'loader'
        ? 'Loader'
        : statusLower === 'unauthorized'
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
    const button = document.getElementById('refresh-devices-btn');
    if (button?.disabled) return;
    if (button) {
        button.disabled = true;
        button.textContent = '刷新中…';
        button.setAttribute('aria-busy', 'true');
    }
    showToast('正在刷新设备列表...', 'info');
    try {
        // 集群模式下触发真正的 Worker 端 refresh_devices（重新 adb devices
        // 并回写 Controller）。该命令最长可达 Worker 命令超时（15s+），不能
        // 阻塞手动刷新的关键路径：以后台任务方式执行，完成后再静默合并
        // 最新快照；失败只记日志（随后 loadDevices 读取的仍是回写前缓存）。
        // silentToast：后台请求失败绝不允许弹出错误 toast 覆盖用户
        // 正在阅读的操作反馈（如启动测试的 409 容量提示）。
        if (state.clusterStatus?.enabled && workspaceWorkerId()) {
            const refreshedWorker = workspaceWorkerId();
            apiCall(
                `/api/cluster/workers/${encodeURIComponent(refreshedWorker)}/refresh`,
                'POST',
                null,
                {silentToast: true})
                .then(() => {
                    if (workspaceWorkerId() !== refreshedWorker) return;
                    // refresh 回包是集群快照形状（id/state/properties），
                    // 与 /api/devices/list 的 device_id/status 形状不同；
                    // 直接赋值渲染会和随后的 loadDevices 各重建一次 DOM，
                    // 造成手动刷新闪屏。这里只触发一次静默重拉（refresh
                    // 刚回写了 Worker 端扫描结果，重拉必然拿到新数据），
                    // 由统一的渲染签名守卫去重。
                    loadDevices(true, {silent: true}).catch(() => {});
                })
                .catch(workerError => {
                    debugLog(`[refreshDevices] worker refresh fallback: ${workerError.message}`);
                });
        }
        // 手动刷新时强制绕过缓存，并标记来源为手动。
        await loadDevices(true, {source: 'manual'});
        showToast('设备列表已刷新', 'success');
    } catch (error) {
        showToast(`刷新设备失败: ${error.message}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = '↻ 刷新设备';
            button.removeAttribute('aria-busy');
        }
    }
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
    if (!validateRebootDeviceSelection()) return;

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
        addWorkerLog(workerId, `正在重启 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('设备正在重启', 'success');
        if (state.usbipConnected) {
            scheduleUsbipReconnect('USB/IP 设备正在重启');
        }
    } catch (error) {
        addLogEntry('重启设备失败: ' + error.message, 'error');
    }
}

async function remountDevices() {
    const devices = selectedTestDeviceIds();
    if (!devices.length) {
        showToast('请重新选择要操作的设备', 'warning');
        return;
    }
    const button = document.getElementById('btn-remount-devices');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        const workerId = selectedClusterWorker();
        addWorkerLog(workerId, '正在执行 remount...', 'info');
        if (workerId) {
            await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices, action: 'remount'
            });
        } else {
            await apiCall('/api/devices/remount', 'POST', {devices});
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

        const workerId = selectedClusterWorker();
        addWorkerLog(workerId, `正在连接 Wi-Fi (${ssid})...`, 'info');
        showToast('正在连接 Wi-Fi...', 'info');

        await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/wifi', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices),
                        action: 'wifi', ssid, password}
                     : {devices: Array.from(state.selectedDevices), ssid, password});

        addWorkerLog(workerId, `Wi-Fi 连接命令已发送 (${ssid})`, 'success');
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
        const workerId = selectedClusterWorker();
        addWorkerLog(workerId, `正在${actionText}设备...`, 'info');
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
        addWorkerLog(workerId, `设备${actionText}完成`, 'info');
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
        addWorkerLog(workerId, '设备锁定状态: ' + JSON.stringify(result, null, 2), 'info');
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
        addWorkerLog(workerId, '设备信息: ' + JSON.stringify(result, null, 2), 'info');
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
