// WebSocket lifecycle and server event dispatch extracted from navigation.js.
// FastAPI WebSocket 连接。
// 会话失效（401/4401）或服务端不可达时按指数退避重连：固定 5 秒重试
// 会让未登录的后台标签页无限空转，持续消耗本机连接资源。
let wsReconnectAttempts = 0;

function scheduleWebSocketReconnect() {
    if (state.websocketReconnectTimer) return;
    const delay = Math.min(5000 * 2 ** Math.min(wsReconnectAttempts, 4), 60000);
    wsReconnectAttempts += 1;
    state.websocketReconnectTimer = setTimeout(() => {
        state.websocketReconnectTimer = null;
        debugLog('[WebSocket] Attempting to reconnect...');
        initWebSocket();
    }, delay);
}

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

    // 获取客户端ID（后台链路：401 不弹登录层，尊重用户手动关闭）
    apiCall('/api/users/current', 'GET', null, {background: true}).then(data => {
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
            wsReconnectAttempts = 0;
            debugLog('[WebSocket] Connected');
            updateConnectionStatus(true);
            // 显示可读的 username@ip，而非平台用户安全边界（裸 ID）。
            const displayId = data.display_client_id || clientId;
            addLogEntry(`WebSocket已连接 (${displayId})`, 'success');
            // WebSocket 成为实时主通道后，把增量游标对齐到服务端当前日志总数，
            // 避免断连期间的轮询已显示的日志在重连后被再次补发。
            // F5 刷新时 state.testing 可能尚未置位（checkInitialTestStatus 有延迟），
            // 因此不依赖 state.testing，只要服务端有日志就对齐游标。
            apiCall('/api/test/status?logs=false', 'GET', null, {background: true}).then(s => {
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
            // 指数退避重连：5s → 10s → 20s → 40s → 60s（上限），成功后归零。
            scheduleWebSocketReconnect();
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
                                // 被占用设备保留在选中集合中，仅由渲染层隐藏勾选，
                                // 解除占用后勾选自动恢复。这里只更新锁定状态。
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
        // 获取身份失败（如会话失效 401）同样走退避重连，登录成功后会整页刷新。
        scheduleWebSocketReconnect();
    });
}

// ==================== Server Event Handler ====================
// Dispatches resource events pushed by the backend EventBus through WebSocket.
// Each handler refreshes the relevant UI state without a full polling cycle.

function handleServerEvent(eventType, payload) {
    debugLog('[EventBus] Received:', eventType, payload);
    switch (eventType) {
        case 'worker.updated':
            // Heartbeats from every Worker emit this event. The selector
            // belongs to the test page, so refreshing it while the user is
            // on desktop/terminal/reports turns every heartbeat into an
            // unnecessary /api/cluster/workers request.
            if (currentPage === 'test' && typeof loadClusterWorkers === 'function') {
                loadClusterWorkers(true).catch(() => {});
            }
            break;
        case 'worker.availability_changed': {
            // Emitted only on real online/offline flips (not per heartbeat).
            // Surface it through the notification center, a toast and, when
            // the tab is hidden, a browser notification.
            const workerId = String(payload?.worker_id || '');
            if (!workerId) break;
            const name = String(payload?.name || '');
            const label = name && name !== workerId ? `${name}（${workerId}）` : workerId;
            if (payload?.status === 'offline') {
                handleRealtimeNotification({
                    title: '主机离线',
                    message: `${label} 心跳超时，已标记为离线`,
                    level: 'warning',
                    category: 'cluster',
                    data: { worker_id: workerId, status: 'offline' }
                });
            } else if (payload?.status === 'online') {
                handleRealtimeNotification({
                    title: '主机上线',
                    message: `${label} 已重新上线`,
                    level: 'success',
                    category: 'cluster',
                    data: { worker_id: workerId, status: 'online' }
                });
            }
            break;
        }
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


