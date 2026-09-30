async function setupUsbipForward() {
    const btn = $('usbip-btn');
    if (!btn) return;

    if (btn.disabled) return;
    debugLog('[setupUsbipForward] Called, state.usbipConnected =', state.usbipConnected);
    const granted = await requestElevatedAccess('管理USB/IP设备接入');
    if (!granted) return;
    await openUsbipAttachModal();
}

function usbipSelectionSerials(group, busid) {
    const mapped = group?.device_serials_by_busid?.[busid];
    const values = Array.isArray(mapped) ? mapped : (group?.device_serials || []);
    return Array.from(new Set(values.map(value => String(value || '').trim()).filter(Boolean)));
}

function usbipSourceOsLabel(sourceOs) {
    return {windows: 'Windows', ubuntu: 'Ubuntu'}[String(sourceOs || '').trim()] || '';
}

function usbipAssignmentLabel(selection, busid) {
    const serials = usbipSelectionSerials(selection, busid);
    const rawStatus = selection?.statuses_by_busid?.[busid]
        || selection?.status
        || 'attached';
    const statusLabels = {
        attaching: '正在接入',
        attached: '已接入',
        unknown: '状态待确认',
        cleanup_required: '需断开清理',
        detaching: '正在断开',
    };
    const osLabel = usbipSourceOsLabel(selection?.source_os);
    return (
        `${selection.device_host} → ${selection.worker_id || 'Controller'} · ${busid}`
        + `｜设备：${serials.join('、') || '尚未识别'}`
        + (osLabel ? `｜来源：${osLabel}` : '')
        + `｜${statusLabels[rawStatus] || rawStatus}`
    );
}

function usbipAssignmentOperationKey(selection) {
    const host = String(selection?.device_host || '');
    const worker = String(selection?.worker_id || workspaceLocalWorkerId());
    const busids = (selection?.busids || [])
        .map(value => String(value || '').trim())
        .filter(Boolean)
        .sort()
        .join(',') || '*';
    return `${host}|${worker}|${busids}`;
}

function updateUsbipAssignmentOperationButtons() {
    document.querySelectorAll('[data-usbip-operation-key]').forEach(button => {
        const pending = usbipPendingAssignmentKeys.has(
            button.dataset.usbipOperationKey
        );
        const detaching = button.dataset.usbipDetaching === 'true';
        button.disabled = usbipRoutingOperationRunning || pending || detaching;
        button.textContent = pending || detaching
            ? '断开中...'
            : button.dataset.usbipIdleLabel || '断开';
    });
}

async function refreshUsbipAssignments() {
    await loadUsbipAssignments();
}

// 新建接入区：强制重新枚举当前选中来源主机的USB设备（绕过5秒缓存）
// 并同步来源系统标识。
async function refreshUsbipSourceDevices() {
    const source = document.getElementById('usbip-source-host')?.value || '';
    if (!source) {
        showToast('请先选择设备来源', 'warning');
        return;
    }
    usbipSourceDeviceCache.delete(source);
    await Promise.all([
        loadUsbipSourceDevices(true),
        refreshUsbipSourceOsLabels([source]),
    ]);
}

async function loadUsbipAssignments() {
    const container = document.getElementById('usbip-assignments');
    if (!container) return;
    const hadRenderedAssignments = container.dataset.loaded === 'true';
    container.setAttribute('aria-busy', 'true');
    if (!hadRenderedAssignments) container.textContent = '正在读取接入状态...';
    try {
        const showAll = document.getElementById('usbip-show-all-assignments')?.checked;
        let rows = [];
        let connected = false;
        let statusSource = '';
        if (showAll) {
            // 显示全部：聚合所有来源主机的接入，并做一次本地 usbip port
            // 实时核对（区分"已记录分配"与"当前已连接"）。
            const data = await apiCall('/api/usbip/assignments?verify=true', 'GET');
            (data.cluster_selections || []).forEach(group => {
                (group.busids || []).forEach(busid => {
                    rows.push({
                        ...group,
                        busids: [busid],
                        device_serials: usbipSelectionSerials(group, busid),
                    });
                });
            });
            connected = rows.length > 0;
        } else {
            // 默认：跟随弹框中选中的设备来源；未选择来源时回退到
            // 本会话最近接入的主机，避免查询成操作者自身主机而显示为空。
            const modalSource = document.getElementById('usbip-source-host')?.value || '';
            const statusHost = modalSource || pendingUsbipDeviceHost;
            const statusPath = statusHost
                ? '/api/usbip/status?device_host=' + encodeURIComponent(statusHost)
                : '/api/usbip/status';
            const status = await apiCall(statusPath, 'GET');
            const selections = status.cluster_selections || [];
            statusSource = status.device_host || statusHost || '';
            connected = Boolean(status.connected);
            if (statusSource) usbipAssignedBusidsBySource.set(statusSource, new Set());
            selections.forEach(group => {
                const assignedBusids = usbipAssignedBusidsBySource.get(group.device_host)
                    || new Set();
                (group.busids || []).forEach(busid => {
                    assignedBusids.add(busid);
                    rows.push({
                        ...group,
                        busids: [busid],
                        device_serials: usbipSelectionSerials(group, busid),
                    });
                });
                usbipAssignedBusidsBySource.set(group.device_host, assignedBusids);
            });
            if (!rows.length && status.connected && activeUsbipSelection?.busids?.length) {
                activeUsbipSelection.busids.forEach(busid => {
                    rows.push({...activeUsbipSelection, busids: [busid]});
                });
            }
        }
        container.replaceChildren();
        rows.forEach(selection => {
            const busid = selection.busids[0];
            const row = document.createElement('div');
            row.className = 'adb-proxy-assignment';
            const assignmentStatus = selection?.statuses_by_busid?.[busid]
                || selection?.status
                || 'attached';
            if (['attaching', 'attached', 'unknown', 'cleanup_required', 'detaching'].includes(assignmentStatus)) {
                row.classList.add(`routing-status-${assignmentStatus}`);
            }
            const transportState = selection?.transport_state_by_busid?.[busid] || '';
            if (transportState === 'detached') {
                // 已记录分配但本机 usbip port 已无该 (host, busid) 会话。
                row.classList.add('routing-status-cleanup_required');
            }
            const info = document.createElement('div');
            info.className = 'adb-proxy-assignment-info';
            info.textContent = usbipAssignmentLabel(selection, busid);
            if (transportState) {
                const transportBadge = document.createElement('span');
                transportBadge.className = 'usbip-transport-state';
                const transportLabels = {
                    attached: '✓ 已连接',
                    detached: '⚠ 已断开（记录残留）',
                    unknown: '实时状态未知',
                };
                transportBadge.textContent = `［${transportLabels[transportState] || transportState}］`;
                transportBadge.title = '已记录分配与本机 usbip port 实时核对结果；'
                    + '记录残留表示分配仍在但传输已不在（如 Worker 重启或手工 detach）';
                info.append(transportBadge);
            }
            const actions = document.createElement('div');
            actions.className = 'device-routing-actions';
            if (['unknown', 'cleanup_required'].includes(assignmentStatus)) {
                const inspectFailure = document.createElement('button');
                inspectFailure.type = 'button';
                inspectFailure.className = 'btn-xxs';
                inspectFailure.textContent = '查看原因';
                inspectFailure.addEventListener('click', () => showUsbipDiagnostics(selection));
                actions.append(inspectFailure);
            }
            const disconnect = document.createElement('button');
            disconnect.type = 'button';
            disconnect.className = 'btn-xxs btn-danger';
            const idleLabel = assignmentStatus === 'cleanup_required'
                ? '清理' : assignmentStatus === 'unknown' ? '强制断开' : '断开';
            disconnect.textContent = idleLabel;
            disconnect.dataset.usbipOperationKey = usbipAssignmentOperationKey(selection);
            disconnect.dataset.usbipIdleLabel = idleLabel;
            disconnect.dataset.usbipDetaching = String(assignmentStatus === 'detaching');
            disconnect.addEventListener('click', async () => {
                await performUsbipDisconnect([selection]);
            });
            actions.append(disconnect);
            row.append(info, actions);
            container.append(row);
        });
        if (!rows.length && !showAll && connected) {
            const legacy = {
                device_host: statusSource || pendingUsbipDeviceHost,
                worker_id: workspaceLocalWorkerId(),
            };
            const row = document.createElement('div');
            row.className = 'adb-proxy-assignment';
            const info = document.createElement('div');
            info.className = 'adb-proxy-assignment-info';
            info.textContent = `${legacy.device_host}｜历史USB/IP接入（无端口记录）`;
            const disconnect = document.createElement('button');
            disconnect.type = 'button';
            disconnect.className = 'btn-xxs btn-danger';
            disconnect.textContent = '断开';
            disconnect.dataset.usbipOperationKey = usbipAssignmentOperationKey(legacy);
            disconnect.dataset.usbipIdleLabel = '断开';
            disconnect.dataset.usbipDetaching = 'false';
            disconnect.addEventListener('click', async () => {
                await performUsbipDisconnect([legacy]);
            });
            row.append(info, disconnect);
            container.append(row);
        }
        if (!container.children.length) {
            container.textContent = showAll
                ? '当前没有任何通过USB/IP接入的设备。'
                : '当前来源没有通过USB/IP接入的设备。';
        }
        container.dataset.loaded = 'true';
        state.usbipConnected = Boolean(rows.length || connected);
        updateUsbipButtonStatus(state.usbipConnected);
        updateUsbipAssignmentOperationButtons();
    } catch (error) {
        if (hadRenderedAssignments) showToast(`USB/IP接入状态刷新失败: ${error.message}`, 'error');
        else container.textContent = `读取USB/IP接入状态失败：${error.message}`;
    } finally {
        container.setAttribute('aria-busy', 'false');
    }
}

async function showUsbipDiagnostics(selection) {
    const {modal, modalId} = createAnalysisModal(
        'usbip-diagnostics',
        'USB/IP 诊断',
        '正在读取传输、协议和网络质量状态...'
    );
    // 须在首个 await 前注册：加载期间关闭时 close() 才能移除弹窗节点。
    ModalManager.onClose(modalId, () => modal.remove());
    try {
        const status = await apiCall(
            '/api/usbip/status?device_host=' + encodeURIComponent(selection.device_host),
            'GET'
        );
        const body = modal.querySelector('.modal-body');
        body.replaceChildren();
        const output = document.createElement('pre');
        output.className = 'transport-diagnostics-output';
        output.textContent = JSON.stringify(status, null, 2);
        const download = document.createElement('button');
        download.type = 'button';
        download.className = 'btn-xxs btn-primary';
        download.textContent = '导出诊断 JSON';
        download.addEventListener('click', () => {
            const blob = new Blob([JSON.stringify(status, null, 2)], {
                type: 'application/json'
            });
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = `usbip-diagnostics-${Date.now()}.json`;
            anchor.click();
            URL.revokeObjectURL(url);
        });
        body.append(download, output);
    } catch (error) {
        showModalError(modal, error.message);
    }
}

async function performUsbipDisconnect(selections) {
    const operationKeys = Array.from(new Set(
        (selections || []).map(usbipAssignmentOperationKey)
    ));
    if (!operationKeys.length) return;
    if (
        usbipRoutingOperationRunning
        || operationKeys.some(key => usbipPendingAssignmentKeys.has(key))
    ) {
        showToast('USB/IP操作正在进行，请等待完成', 'warning');
        return;
    }
    const btn = $('usbip-btn');
    const operationGeneration = ++usbipOperationGeneration;
    usbipRoutingOperationRunning = true;
    operationKeys.forEach(key => usbipPendingAssignmentKeys.add(key));
    updateUsbipAssignmentOperationButtons();
    try {
        btn.textContent = '📱 断开中...';
        btn.disabled = true;
        usbipManualDisconnectUntil = Date.now() + USBIP_MANUAL_DISCONNECT_SUPPRESS_MS;
        if (usbipReconnectTimer) {
            clearTimeout(usbipReconnectTimer);
            usbipReconnectTimer = null;
        }
        const workerBaselines = new Map();
        const expectedUsbipSerials = new Map();
        selections.forEach(selection => {
            const workerId = selection.worker_id || workspaceLocalWorkerId();
            if (!workerBaselines.has(workerId)) {
                const serials = new Set(
                    (state.devices || [])
                        .filter(device => (
                            device.worker_id === workerId
                            || String(device.device_id || '').startsWith(`${workerId}:`)
                            || (
                                workerId === workspaceLocalWorkerId()
                                && !device.worker_id
                            )
                        ))
                        .map(device => String(device.serial || device.device_id || '').split(':').pop())
                );
                workerBaselines.set(workerId, serials);
            }
            const expected = expectedUsbipSerials.get(workerId) || new Set();
            (selection.device_serials || []).forEach(serial => {
                if (serial) expected.add(String(serial));
            });
            expectedUsbipSerials.set(workerId, expected);
        });
        for (const selection of selections) {
            const disconnectPayload = {
                device_host: selection.device_host,
                source_host: selection.source_host || '',
                worker_id: selection.worker_id || '',
                busids: selection.busids || [],
            };
            const result = await apiCall(
                '/api/usbip/disconnect',
                'POST',
                disconnectPayload
            );
            addLogEntry(result.message || 'USB/IP设备已断开', 'success');
            const workerId = selection.worker_id || workspaceLocalWorkerId();
            const expected = expectedUsbipSerials.get(workerId) || new Set();
            (result.removed_devices || []).forEach(serial => {
                if (serial) expected.add(String(serial));
            });
            expectedUsbipSerials.set(workerId, expected);
            if (Array.isArray(result.remaining_devices) && result.remaining_devices.length) {
                addLogEntry('断开后仍在线: ' + result.remaining_devices.join(' '), 'warning');
            }
        }
        activeUsbipSelection = null;
        selections.forEach(selection => {
            usbipSourceDeviceCache.delete(selection.device_host);
        });
        await loadUsbipAssignments();
        // Source USB enumeration and the global ADB refresh can take tens of
        // seconds after a detach.  The backend has already confirmed the
        // operation, so release the UI now and finish those reads in the
        // background instead of holding the connect button disabled.
        void refreshUsbipAfterDisconnect(
            workerBaselines,
            expectedUsbipSerials,
            operationGeneration
        );
        setTimeout(() => {
            if (operationGeneration === usbipOperationGeneration) {
                checkUsbipStatus();
            }
        }, 500);
    } catch (error) {
        btn.textContent = '📱 断开设备';
        btn.disabled = false;
        addLogEntry('停止 USB/IP 失败: ' + error.message, 'error');
    } finally {
        operationKeys.forEach(key => usbipPendingAssignmentKeys.delete(key));
        usbipRoutingOperationRunning = false;
        if (btn) btn.disabled = false;
        updateUsbipAssignmentOperationButtons();
    }
}

async function refreshUsbipAfterDisconnect(
    workerBaselines,
    expectedUsbipSerials,
    operationGeneration
) {
    await Promise.allSettled([
        loadUsbipSourceDevices(true, {
            silent: true,
            preserveSelection: true,
        }),
        loadDevices(true, {silent: true}),
    ]);
    if (operationGeneration !== usbipOperationGeneration) return;
    await refreshUsbipDetachedWorkers(
        workerBaselines,
        expectedUsbipSerials,
        operationGeneration
    );
}

async function refreshUsbipDetachedWorkers(
    workerBaselines,
    expectedUsbipSerials = new Map(),
    operationGeneration = usbipOperationGeneration
) {
    // Compatibility with older cached callers that passed generation second.
    if (typeof expectedUsbipSerials === 'number') {
        operationGeneration = expectedUsbipSerials;
        expectedUsbipSerials = new Map();
    }
    for (const delay of [2000, 3000, 5000, 8000, 12000, 15000]) {
        await new Promise(resolve => setTimeout(resolve, delay));
        if (operationGeneration !== usbipOperationGeneration) return;
        let changed = false;
        for (const [workerId, baseline] of workerBaselines.entries()) {
            try {
                const devices = await fetchDevicesForWorker(workerId, true);
                const visible = new Set(
                    (devices || []).map(device => String(device.serial || device.device_id || '').split(':').pop())
                );
                const expected = expectedUsbipSerials.get(workerId) || new Set();
                const usbipVisible = new Set(
                    (devices || [])
                        .filter(device => (
                            device.transport === 'usbip'
                            || device.is_usbip === true
                            || device.properties?.is_usbip === true
                        ))
                        .map(device => String(device.serial || device.device_id || '').split(':').pop())
                );
                if (
                    (expected.size && ![...expected].some(serial => usbipVisible.has(serial)))
                    || (
                        !expected.size
                        && baseline.size
                        && [...baseline].some(serial => !visible.has(serial))
                    )
                ) {
                    const stillOnlineElsewhere = [...expected].filter(serial => (
                        visible.has(serial) && !usbipVisible.has(serial)
                    ));
                    if (stillOnlineElsewhere.length) {
                        addLogEntry(
                            'USB/IP已断开；同序列号设备仍通过其他ADB传输在线: '
                            + stillOnlineElsewhere.join(' '),
                            'warning'
                        );
                    }
                    changed = true;
                    break;
                }
            } catch (error) {
                debugLog('[USB/IP] Detached Worker refresh failed:', error.message);
            }
        }
        if (operationGeneration !== usbipOperationGeneration) return;
        if (changed) {
            await loadDevices(true);
            if (operationGeneration !== usbipOperationGeneration) return;
            addLogEntry('已自动刷新设备列表，USB/IP设备已从ADB移除', 'success');
            return;
        }
    }
    if (operationGeneration !== usbipOperationGeneration) return;
    await loadDevices(true);
    if (operationGeneration !== usbipOperationGeneration) return;
    addLogEntry('USB/IP已断开，但ADB设备状态更新较慢，已完成最终刷新', 'warning');
}

async function openUsbipAttachModal() {
    const sourceSelect = document.getElementById('usbip-source-host');
    const targetSelect = document.getElementById('usbip-target-worker');
    const message = document.getElementById('usbip-attach-message');
    const submit = document.getElementById('usbip-attach-submit');
    if (!sourceSelect || !targetSelect) return;

    const config = state.config || {};
    const sources = new Set();
    const isLoopbackSource = value => {
        const rawHost = String(value || '').split('@').pop().replace(/^\[|\]$/g, '');
        if (rawHost.toLowerCase() === '::1') return true;
        const host = rawHost.split(':')[0];
        return ['127.0.0.1', 'localhost', '::1'].includes(host.toLowerCase());
    };
    [config.usbip_device_host, config.device_host, pendingUsbipDeviceHost]
        .filter(value => value && String(value).includes('@') && !isLoopbackSource(value))
        .forEach(value => sources.add(String(value)));
    Object.entries(config.client_hosts || {}).forEach(([host, username]) => {
        if (host && username && !isLoopbackSource(`${username}@${host}`)) {
            sources.add(`${username}@${host}`);
        }
    });
    sourceSelect.innerHTML = '';
    if (!sources.size) {
        sourceSelect.append(new Option('未配置设备来源', ''));
    } else {
        sources.forEach(value => sourceSelect.append(new Option(value, value)));
        // 打开弹窗即显示已缓存的来源系统标识，未知的后台探测补齐。
        usbipSourceOsByHost.forEach((osValue, host) => {
            applyUsbipSourceOsLabel(host, osValue);
        });
        void refreshUsbipSourceOsLabels([...sources]);
    }

    const localWorkerId = workspaceLocalWorkerId();
    targetSelect.innerHTML = '';
    targetSelect.append(new Option(localWorkerId, localWorkerId));
    try {
        const response = await fetch('/api/cluster/hosts', {
            credentials: 'same-origin',
            cache: 'no-store'
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        (payload.hosts || []).forEach(host => {
            if (!host.worker_id || host.worker_id === localWorkerId) return;
            const option = new Option(host.worker_id, host.worker_id);
            const online = ['online', 'busy'].includes(host.status);
            const usbipCapable = host.capabilities?.usbip_client === true;
            option.disabled = !online || !usbipCapable;
            if (!online) option.textContent += '（离线）';
            else if (!usbipCapable) option.textContent += '（需重新部署以启用USB/IP）';
            targetSelect.append(option);
        });
    } catch (error) {
        debugLog('[USB/IP] Failed to load cluster hosts:', error.message);
        if (message) message.textContent = `加载集群主机失败：${error.message}；仍可接入 Controller。`;
    }
    const preferredWorker = workspaceWorkerId();
    targetSelect.value = Array.from(targetSelect.options)
        .some(option => option.value === preferredWorker && !option.disabled)
        ? preferredWorker : localWorkerId;
    if (submit) submit.disabled = !sourceSelect.value;
    ModalManager.open('usbip-attach-modal');
    await loadUsbipAssignments();
    await loadUsbipSourceDevices();
    // Source enumeration also repairs older assignments that were persisted
    // before ADB exposed their serial. Refresh the current rows afterwards.
    await loadUsbipAssignments();
    startUsbipSourceRefresh();
}

function closeUsbipAttachModal() {
    stopUsbipSourceRefresh();
    ModalManager.close('usbip-attach-modal');
}

function stopUsbipSourceRefresh() {
    if (usbipSourceRefreshTimer) clearTimeout(usbipSourceRefreshTimer);
    usbipSourceRefreshTimer = null;
    usbipSourceRefreshRunning = false;
    usbipSourceUnauthorizedStreak = 0;
}

// 来源主机连续未授权（缺 SSH 凭据）时拉长静默轮询间隔，避免控制台 401 刷屏。
const USBIP_SOURCE_REFRESH_BACKOFF_MS = 30000;
const USBIP_SOURCE_REFRESH_BACKOFF_STREAK = 3;
let usbipSourceUnauthorizedStreak = 0;

function startUsbipSourceRefresh() {
    stopUsbipSourceRefresh();
    const refresh = async () => {
        if (
            !ModalManager.isOpen('usbip-attach-modal')
            || usbipSourceRefreshRunning
            || usbipRoutingOperationRunning
        ) return;
        usbipSourceRefreshRunning = true;
        try {
            await loadUsbipSourceDevices(true, {
                silent: true,
                preserveSelection: true,
            });
        } catch (error) {
            debugLog('[USB/IP] automatic source refresh failed:', error.message);
        } finally {
            usbipSourceRefreshRunning = false;
        }
    };
    // 自调度循环替代固定 setInterval：来源连续未授权时退避到长间隔，
    // 避免 3 秒一次的 401 刷屏；凭据补齐后自动恢复正常节奏。
    const scheduleNext = () => {
        if (!ModalManager.isOpen('usbip-attach-modal')) return;
        const delay = usbipSourceUnauthorizedStreak >= USBIP_SOURCE_REFRESH_BACKOFF_STREAK
            ? USBIP_SOURCE_REFRESH_BACKOFF_MS
            : DEVICE_ROUTING_REFRESH_INTERVAL_MS;
        usbipSourceRefreshTimer = setTimeout(async () => {
            await refresh();
            scheduleNext();
        }, delay);
    };
    scheduleNext();
    ModalManager.onClose('usbip-attach-modal', stopUsbipSourceRefresh);
}

async function submitUsbipAttach() {
    if (usbipRoutingOperationRunning) {
        showToast('USB/IP操作正在进行，请等待完成', 'warning');
        return;
    }
    const deviceHost = document.getElementById('usbip-source-host')?.value || '';
    const workerId = document.getElementById('usbip-target-worker')?.value || '';
    const busids = Array.from(
        document.getElementById('usbip-source-device')?.selectedOptions || []
    ).map(option => option.value).filter(Boolean);
    if (!deviceHost) {
        showToast('请先配置设备来源', 'warning');
        return;
    }
    if (!workerId) {
        showToast('请选择接入主机', 'warning');
        return;
    }
    if (!busids.length) {
        showToast('请至少选择一个USB设备', 'warning');
        return;
    }
    const submit = document.getElementById('usbip-attach-submit');
    const message = document.getElementById('usbip-attach-message');
    if (submit) submit.disabled = true;
    if (message) message.textContent = '正在接入USB/IP设备，请稍候...';
    try {
        await connectUsbipDeviceHost(deviceHost, workerId, busids);
    } finally {
        const sourceDevice = document.getElementById('usbip-source-device');
        if (submit) {
            submit.disabled = (
                !sourceDevice
                || sourceDevice.disabled
                || !sourceDevice.value
            );
        }
    }
}

async function loadUsbipSourceDevices(force = false, options = {}) {
    const source = document.getElementById('usbip-source-host')?.value || '';
    const select = document.getElementById('usbip-source-device');
    const message = document.getElementById('usbip-attach-message');
    if (!select) return;
    const knownBusids = new Set(
        Array.from(select.options || []).map(option => option.value).filter(Boolean)
    );
    const selectedBusids = new Set(
        Array.from(select.selectedOptions || []).map(option => option.value).filter(Boolean)
    );
    if (!options.silent) {
        select.disabled = true;
        select.innerHTML = '<option value="">正在读取USB设备...</option>';
    }
    if (!source) return;
    const cached = usbipSourceDeviceCache.get(source);
    if (!force && cached && Date.now() - cached.timestamp < 5000) {
        renderUsbipSourceDevices(source, cached.devices, {
            ...options,
            knownBusids,
            selectedBusids,
            sourceOs: cached.sourceOs,
        });
        return;
    }
    if (usbipSourceLoadPromise?.source === source) {
        await usbipSourceLoadPromise.promise;
        return;
    }
    const request = apiCall(
        '/api/usbip/source-devices?device_host=' + encodeURIComponent(source),
        'GET',
        null,
        // 静默轮询按后台请求处理：会话/提权失效时不弹登录层或提权框。
        options.silent ? {background: true, silentToast: true} : {}
    );
    usbipSourceLoadPromise = {source, promise: request};
    try {
        const result = await request;
        const devices = result.devices || [];
        const sourceOs = String(result.source_os || '').trim();
        usbipSourceDeviceCache.set(source, {timestamp: Date.now(), devices, sourceOs});
        usbipSourceUnauthorizedStreak = 0;
        renderUsbipSourceDevices(source, devices, {
            ...options,
            knownBusids,
            selectedBusids,
            sourceOs,
        });
    } catch (error) {
        if (options.silent) {
            // 静默轮询：连续未授权（平台会话或来源凭据缺失）时递增连败
            // 计数，驱动 startUsbipSourceRefresh 的退避调度；成功后清零。
            if (error.status === 401) {
                usbipSourceUnauthorizedStreak += 1;
            }
            debugLog('[USB/IP] source device polling failed:', error.message);
        } else if (error.needPassword || error.need_password) {
            // 来源未配置SSH密码：不自动弹密码框打断操作，改为内联提示
            // + 主动按钮（见 showUsbipSourceCredentialPrompt）。
            showUsbipSourceCredentialPrompt(source);
        } else {
            select.innerHTML = '<option value="">USB设备加载失败</option>';
            if (message) message.textContent = `USB设备加载失败：${error.message}`;
            if (error.installGuide) {
                showInstallGuide('usbipd 安装指南', error.installGuide);
            }
        }
    } finally {
        if (usbipSourceLoadPromise?.promise === request) {
            usbipSourceLoadPromise = null;
        }
    }
}

function showUsbipSourceCredentialPrompt(source) {
    const select = document.getElementById('usbip-source-device');
    const message = document.getElementById('usbip-attach-message');
    if (select) {
        select.innerHTML = '<option value="">该来源需要SSH密码后才能列出USB设备</option>';
        select.disabled = true;
    }
    const submit = document.getElementById('usbip-attach-submit');
    if (submit) submit.disabled = true;
    if (!message) return;
    message.replaceChildren();
    const hint = document.createElement('span');
    hint.textContent = `${source} 尚未配置SSH密码。`;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-xxs btn-primary';
    button.style.marginLeft = '8px';
    button.textContent = '输入SSH密码';
    button.addEventListener('click', () => {
        showDevicePasswordModal(source, 'usbip-list', loadUsbipSourceDevices);
    });
    message.append(hint, button);
}

function applyUsbipSourceOsLabel(source, sourceOs) {
    const label = usbipSourceOsLabel(sourceOs);
    if (!source || !label) return;
    usbipSourceOsByHost.set(String(source), String(sourceOs).trim());
    const sourceSelect = document.getElementById('usbip-source-host');
    if (!sourceSelect) return;
    const option = sourceSelect.querySelector(
        `option[value="${CSS.escape(String(source))}"]`
    );
    if (!option) return;
    if (!option.dataset.baseLabel) {
        option.dataset.baseLabel = String(option.textContent)
            .replace(/\s*·\s*(Windows|Ubuntu)$/, '');
    }
    option.textContent = `${option.dataset.baseLabel} · ${label}`;
}

async function refreshUsbipSourceOsLabels(sources) {
    const targets = Array.from(new Set(
        (sources || []).map(value => String(value || '').trim()).filter(Boolean)
    ));
    if (!targets.length) return;
    try {
        const result = await apiCall(
            '/api/usbip/source-os?hosts=' + encodeURIComponent(targets.join(',')),
            'GET',
            null,
            // 标识刷新是辅助信息：任何失败都不弹登录层、不弹错误提示。
            {background: true, silentToast: true}
        );
        Object.entries(result.sources || {}).forEach(([host, info]) => {
            applyUsbipSourceOsLabel(host, info?.source_os);
        });
    } catch (error) {
        debugLog('[USB/IP] source OS label refresh failed:', error.message);
    }
}

function renderUsbipSourceDevices(source, devices, options = {}) {
    if (document.getElementById('usbip-source-host')?.value !== source) return;
    const select = document.getElementById('usbip-source-device');
    const message = document.getElementById('usbip-attach-message');
    if (!select) return;
    applyUsbipSourceOsLabel(source, options.sourceOs);
    const osLabel = usbipSourceOsLabel(
        options.sourceOs || usbipSourceOsByHost.get(source)
    );
    select.innerHTML = '';
    const assignedBusids = usbipAssignedBusidsBySource.get(source) || new Set();
    const availableDevices = devices.filter(device => !assignedBusids.has(device.busid));
    availableDevices.forEach(device => {
        const option = new Option(device.label || device.busid, device.busid);
        option.selected = options.preserveSelection && options.knownBusids?.has(device.busid)
            ? options.selectedBusids.has(device.busid)
            : true;
        select.append(option);
    });
    if (!availableDevices.length) {
        select.append(new Option(
            devices.length ? '该来源设备均已接入' : '未发现Android USB设备',
            ''
        ));
    }
    select.disabled = !availableDevices.length;
    const submit = document.getElementById('usbip-attach-submit');
    if (submit) {
        submit.disabled = (
            usbipRoutingOperationRunning
            || !availableDevices.length
            || !select.value
        );
    }
    if (message) message.textContent = availableDevices.length
        ? `发现 ${availableDevices.length} 个可接入 USB 设备${osLabel ? `（来源系统：${osLabel}）` : ''}。多选时，Windows/Linux 按住 Ctrl，macOS 按住 Command。`
        : devices.length
        ? '该来源当前没有剩余可接入的Android USB设备。'
        : '设备源未发现可接入的Android USB设备。';
}

async function connectUsbipDeviceHost(deviceHost, workerId, busids) {
    if (usbipRoutingOperationRunning) {
        showToast('USB/IP操作正在进行，请等待完成', 'warning');
        return;
    }
    const btn = $('usbip-btn');
    const operationGeneration = ++usbipOperationGeneration;
    usbipRoutingOperationRunning = true;
    updateUsbipAssignmentOperationButtons();
    activeUsbipSelection = {device_host: deviceHost, worker_id: workerId, busids};
    debugLog('[USB/IP] Connecting source:', deviceHost);
    try {
        btn.textContent = '📱 连接中...';
        btn.disabled = true;
        usbipManualDisconnectUntil = 0;
        let targetSerialsBefore = new Set();
        try {
            const devicesBefore = await fetchDevicesForWorker(workerId, true);
            targetSerialsBefore = new Set(
                (devicesBefore || []).map(device => String(device.serial || device.device_id || ''))
            );
        } catch (error) {
            debugLog('[USB/IP] Failed to capture target device baseline:', error.message);
        }
        const result = await apiCall('/api/usbip/connect', 'POST', {
            device_host: deviceHost,
            worker_id: workerId,
            busids,
            manual_connect: true
        });
        if (isUsbipAdbReady(result)) {
            state.usbipConnected = true;
            pendingUsbipDeviceHost = result.device_host || deviceHost;
            activeUsbipSelection.source_host = result.source_host || '';
            activeUsbipSelection.device_serials = (
                result.device_serials || result.new_devices || result.device_list || []
            );
            btn.textContent = '📱 断开设备';
            btn.disabled = false;
            addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
            if (['warning', 'poor'].includes(result.network_quality?.rating)) {
                addLogEntry(
                    `USB/IP网络质量${result.network_quality.rating === 'poor' ? '较差' : '一般'}：`
                    + `RTT ${result.network_quality.average_rtt_ms ?? '-'}ms，`
                    + `丢包 ${result.network_quality.loss_percent ?? '-'}%；`
                    + '完整CTS或大流量操作建议改在来源Worker本地执行',
                    'warning'
                );
            }
            usbipSourceDeviceCache.delete(deviceHost);
            await loadUsbipAssignments();
            await loadUsbipSourceDevices(true);
            await loadUsbipAssignments();
            refreshUsbipTargetWorker(
                workerId,
                result.device_serials || result.new_devices || [],
                targetSerialsBefore,
                operationGeneration
            );
            return;
        }
        btn.textContent = '📱 本地设备';
        btn.disabled = false;
        if (result.need_password && result.device_host) {
            showDevicePasswordModal(result.device_host);
            addLogEntry('需要输入SSH密码以连接到 ' + result.device_host, 'warning');
        } else if (result.error && result.error.includes('SSH连接失败')) {
            addLogEntry('⚠️ SSH 连接失败，请点击 "📡 检查SSHD" 按钮检查SSH服务状态', 'warning');
        } else if (result.install_guide) {
            showInstallGuide('usbipd 安装指南', result.install_guide);
            addLogEntry('启动 USB/IP 失败: ' + (result.error || '未知错误'), 'error');
        } else {
            activeUsbipSelection = null;
            const remediation = result.remediation ? `；建议：${result.remediation}` : '';
            addLogEntry(
                '启动 USB/IP 失败: '
                + (result.error || result.message || '未知错误')
                + remediation,
                'error'
            );
        }
    } catch (error) {
        btn.textContent = '📱 本地设备';
        btn.disabled = false;
        if (error.needPassword && error.deviceHost) {
            showDevicePasswordModal(error.deviceHost);
            addLogEntry('需要输入SSH密码以连接到 ' + error.deviceHost, 'warning');
        } else if (error.installGuide) {
            showInstallGuide('usbipd 安装指南', error.installGuide);
            activeUsbipSelection = null;
        } else {
            activeUsbipSelection = null;
        }
        const remediation = error.remediation ? `；建议：${error.remediation}` : '';
        addLogEntry('启动 USB/IP 失败: ' + error.message + remediation, 'error');
    } finally {
        usbipRoutingOperationRunning = false;
        updateUsbipAssignmentOperationButtons();
    }
}

async function refreshUsbipTargetWorker(
    workerId,
    expectedSerials = [],
    serialsBefore = new Set(),
    operationGeneration = usbipOperationGeneration
) {
    if (workerId && workerId !== workspaceWorkerId()) {
        window.GmsWorkspace?.update({
            scope_mode: isLocalWorkspaceWorker(workerId) ? 'single' : 'cluster',
            worker_id: workerId,
            device_ids: []
        }, {source: 'usbip-attach'});
        syncWorkspaceWorkerSelectors(workerId);
        updateTestHostScopedControls(workerId);
    }
    const baseline = serialsBefore instanceof Set ? serialsBefore : new Set(serialsBefore || []);
    for (const delay of [1000, 3000, 6000, 10000, 15000]) {
        await new Promise(resolve => setTimeout(resolve, delay));
        if (operationGeneration !== usbipOperationGeneration) return;
        try {
            await loadDevices(true);
            if (operationGeneration !== usbipOperationGeneration) return;
            const visible = new Set(
                state.devices.map(device => (
                    String(device.serial || device.device_id || '').split(':').pop()
                ))
            );
            const discoveredSerials = expectedSerials.length
                ? expectedSerials.filter(serial => visible.has(serial))
                : [...visible].filter(serial => !baseline.has(serial));
            if (
                expectedSerials.length
                ? expectedSerials.every(serial => visible.has(serial))
                : [...visible].some(serial => !baseline.has(serial))
            ) {
                addLogEntry(
                    `已刷新 ${workerId} 设备列表，ADB在线：`
                    + (discoveredSerials.join(', ') || '序列号尚未识别'),
                    'success'
                );
                return;
            }
        } catch (error) {
            debugLog('[USB/IP] Target Worker refresh failed:', error.message);
        }
    }
    if (operationGeneration !== usbipOperationGeneration) return;
    addLogEntry(
        `USB/IP传输已连接，设备：${expectedSerials.join(', ') || '尚未识别'}；`
        + `${workerId} 尚未完成ADB枚举，请稍后刷新`,
        'warning'
    );
}

function scheduleUsbipReconnect(reason) {
    if (Date.now() <= usbipManualDisconnectUntil) return;
    if (usbipReconnectWaiting || usbipReconnectTimer) return;
    usbipReconnectWaiting = true;
    usbipReconnectAttempts = 0;
    const btn = $('usbip-btn');
    if (btn) {
        btn.textContent = '📱 等待重连...';
        btn.disabled = false;
    }
    addLogEntry((reason || '检测到 USB/IP 设备断开') + '，等待后端自动重连...', 'warning');
    usbipReconnectTimer = setTimeout(attemptUsbipReconnect, USBIP_RECONNECT_INITIAL_DELAY_MS);
}

function isUsbipAdbReady(result) {
    return !!(result && result.success && (result.transport_connected || (Array.isArray(result.device_list) && result.device_list.length > 0)));
}

function isUsbipProtocolVisible(status) {
    return !!(status?.transport_connected &&
        ['fastboot', 'recovery', 'unauthorized', 'adb_non_device'].includes(status.protocol_status?.mode));
}

async function attemptUsbipReconnect() {
    // 手动断开后立即终止重连循环——不要继续"自动重连等待"。
    // （scheduleUsbipReconnect 的入口守卫拦不住已在执行的循环，故在此复核。）
    if (Date.now() <= usbipManualDisconnectUntil) {
        usbipReconnectTimer = null;
        usbipReconnectWaiting = false;
        const btn = $('usbip-btn');
        if (btn) { btn.textContent = '📱 本地设备'; btn.disabled = false; }
        addLogEntry('已手动断开 USB/IP，停止自动重连', 'info');
        return;
    }
    const btn = $('usbip-btn');
    usbipReconnectAttempts += 1;
    try {
        usbipReconnectTimer = null;
        const statusPath = pendingUsbipDeviceHost
            ? '/api/usbip/status?device_host=' + encodeURIComponent(pendingUsbipDeviceHost)
            : '/api/usbip/status';
        const status = await apiCall(statusPath, 'GET');
        const devices = await loadDevices(true);
        const usbipDevices = devices.filter(device => device && device.is_usbip);
        if (status.connected && (status.adb_ready || usbipDevices.length > 0 || isUsbipProtocolVisible(status)
                || (status.transport_connected && status.reconnecting))) {
            // Flash-mode transition: transport can be ready before ADB.
            state.usbipConnected = true;
            usbipReconnectWaiting = false;
            pendingUsbipDeviceHost = status.device_host || pendingUsbipDeviceHost || '';
            if (btn) {
                btn.textContent = '📱 断开设备';
                btn.disabled = false;
            }
            const protocolMode = status.protocol_status && status.protocol_status.mode;
            addLogEntry(protocolMode && protocolMode !== 'adb'
                ? `USB/IP 后端自动重连已恢复，当前状态: ${protocolMode}`
                : 'USB/IP 后端自动重连已恢复', 'success');
            return;
        }
        throw new Error(status.reconnecting ? '后端正在重连' : '设备尚未稳定在线');
    } catch (error) {
        if (Date.now() <= usbipManualDisconnectUntil) {
            usbipReconnectTimer = null;
            usbipReconnectWaiting = false;
            if (btn) { btn.textContent = '📱 本地设备'; btn.disabled = false; }
            addLogEntry('已手动断开 USB/IP，停止自动重连', 'info');
            return;
        }
        if (usbipReconnectAttempts < USBIP_RECONNECT_MAX_ATTEMPTS) {
            addLogEntry(`USB/IP 自动重连等待第 ${usbipReconnectAttempts} 次未恢复，继续等待...`, 'warning');
            usbipReconnectTimer = setTimeout(attemptUsbipReconnect, USBIP_RECONNECT_INTERVAL_MS);
            return;
        }
        if (btn) {
            btn.textContent = '📱 本地设备';
            btn.disabled = false;
        }
        state.usbipConnected = false;
        usbipReconnectWaiting = false;
        addLogEntry('USB/IP 自动重连失败: ' + error.message, 'error');
        showToast('USB/IP 自动重连失败', 'error');
    }
}
