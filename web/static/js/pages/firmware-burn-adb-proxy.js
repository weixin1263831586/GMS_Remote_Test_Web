async function setupAdbPortForward() {
    const granted = await requestElevatedAccess('管理ADB');
    if (!granted) return;
    await openAdbProxyModal();
}

async function openAdbProxyModal() {
    const assignments = document.getElementById('adb-proxy-assignments');
    const message = document.getElementById('adb-proxy-message');
    const submit = document.getElementById('adb-proxy-connect-submit');
    if (!assignments || !message || !submit) return;
    const hadRenderedAssignments = assignments.dataset.loaded === 'true';
    assignments.setAttribute('aria-busy', 'true');
    if (!hadRenderedAssignments) assignments.textContent = '正在读取接入状态...';
    message.textContent = '正在读取设备来源和接入主机...';
    submit.disabled = true;
    ModalManager.open('adb-proxy-modal');
    startAdbProxyDeviceRefresh();
    try {
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        state.adbForwardRunning = Boolean(adbProxyStatus.connected);
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        updateAdbProxyButton();
    } catch (error) {
        if (!hadRenderedAssignments) assignments.textContent = '接入状态读取失败';
        message.textContent = `加载ADB接入信息失败：${error.message}`;
        addLogEntry('加载ADB接入信息失败: ' + error.message, 'error');
    } finally {
        assignments.setAttribute('aria-busy', 'false');
    }
}

function closeAdbProxyModal() {
    stopAdbProxyDeviceRefresh();
    ModalManager.close('adb-proxy-modal');
}

function adbProxySelectionSnapshot() {
    const deviceSelect = document.getElementById('adb-proxy-source-devices');
    return {
        sourceWorkerId: document.getElementById('adb-proxy-source-host')?.value || '',
        targetWorkerId: document.getElementById('adb-proxy-target-host')?.value || '',
        knownDeviceSerials: new Set(
            Array.from(deviceSelect?.options || []).map(option => option.value).filter(Boolean)
        ),
        selectedDeviceSerials: new Set(
            Array.from(deviceSelect?.selectedOptions || []).map(option => option.value).filter(Boolean)
        ),
    };
}

function stopAdbProxyDeviceRefresh() {
    if (adbProxyDeviceRefreshTimer) clearInterval(adbProxyDeviceRefreshTimer);
    adbProxyDeviceRefreshTimer = null;
    adbProxyDeviceRefreshRunning = false;
}

function startAdbProxyDeviceRefresh() {
    stopAdbProxyDeviceRefresh();
    const refresh = async () => {
        if (
            !ModalManager.isOpen('adb-proxy-modal')
            || adbProxyDeviceRefreshRunning
            || adbProxyOperationRunning
        ) return;
        adbProxyDeviceRefreshRunning = true;
        const selection = adbProxySelectionSnapshot();
        try {
            adbProxyStatus = await apiCall(
                '/api/adb-forward/status',
                'GET',
                null,
                {background: true, silentToast: true}
            );
            state.adbForwardRunning = Boolean(adbProxyStatus.connected);
            renderAdbProxyAssignments();
            renderAdbProxyHosts(selection);
            updateAdbProxyButton();
        } catch (error) {
            debugLog('[ADB Proxy] automatic source refresh failed:', error.message);
        } finally {
            adbProxyDeviceRefreshRunning = false;
        }
    };
    adbProxyDeviceRefreshTimer = setInterval(
        () => void refresh(),
        DEVICE_ROUTING_REFRESH_INTERVAL_MS
    );
    ModalManager.onClose('adb-proxy-modal', stopAdbProxyDeviceRefresh);
}

function adbProxyHostLabel(host) {
    return host.worker_id || '未知 Worker';
}

function toggleAdbProxyUbuntuSource(forceOpen) {
    const panel = document.getElementById('adb-proxy-ubuntu-source-panel');
    const toggle = document.getElementById('adb-proxy-add-ubuntu-toggle');
    if (!panel || !toggle) return;
    panel.hidden = typeof forceOpen === 'boolean' ? !forceOpen : !panel.hidden;
    toggle.textContent = panel.hidden ? '＋ 添加Ubuntu设备来源' : '收起Ubuntu设备来源';
    if (!panel.hidden) {
        document.getElementById('adb-proxy-ubuntu-host')?.focus();
    }
}

async function deployAdbProxyUbuntuSource() {
    const hostInput = document.getElementById('adb-proxy-ubuntu-host');
    const passwordInput = document.getElementById('adb-proxy-ubuntu-password');
    const button = document.getElementById('adb-proxy-ubuntu-deploy');
    const message = document.getElementById('adb-proxy-message');
    const sshHost = hostInput?.value.trim() || '';
    const password = passwordInput?.value || '';
    if (!/^[A-Za-z0-9._-]+@.+/.test(sshHost)) {
        showToast('SSH主机必须使用 用户名@IP 格式', 'warning');
        return;
    }
    if (!password) {
        showToast('请输入SSH密码', 'warning');
        return;
    }
    if (!button || !message) return;

    let finalMessage = '';
    adbProxyOperationRunning = true;
    button.disabled = true;
    button.textContent = '校验SSH指纹…';
    message.textContent = `正在读取 ${sshHost} 的SSH主机指纹…`;
    try {
        const scan = await apiCall(
            '/api/cluster/workers/ssh-host-key/scan',
            'POST',
            {ssh_host: sshHost}
        );
        const fingerprints = (scan.keys || []).map(
            key => `${key.key_type}  ${key.fingerprint}`
        ).join('\n');
        if (!fingerprints) throw new Error('目标主机没有返回可校验的SSH指纹');
        if (!await showConfirmDialog(
            '确认 SSH 主机指纹',
            `请核对 ${scan.host}:${scan.port} 的SSH指纹：\n\n`
            + `${fingerprints}\n\n确认无误后继续安装。`
        )) {
            throw new Error('已取消Ubuntu来源主机安装');
        }
        await apiCall(
            '/api/cluster/workers/ssh-host-key/trust',
            'POST',
            {ssh_host: sshHost, keys: scan.keys}
        );
        button.textContent = '安装adbproxy-rs…';
        message.textContent = `正在 ${sshHost} 安装adbproxy-rs和来源Agent…`;
        const deployed = await apiCall(
            '/api/cluster/workers/deploy-adb-proxy-source',
            'POST',
            {
                ssh_host: sshHost,
                password,
                controller_url: window.location.origin
            }
        );
        if (passwordInput) passwordInput.value = '';
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        const sourceSelect = document.getElementById('adb-proxy-source-host');
        if (
            sourceSelect
            && Array.from(sourceSelect.options).some(
                option => option.value === deployed.worker_id
            )
        ) {
            sourceSelect.value = deployed.worker_id;
            renderAdbProxySourceDevices();
        }
        toggleAdbProxyUbuntuSource(false);
        finalMessage = (
            `${sshHost} 已安装并添加为ADB设备来源`
            + (deployed.registered ? '，设备清单已同步。' : '。')
        );
        showToast('Ubuntu ADB设备来源添加成功', 'success');
    } catch (error) {
        finalMessage = `添加Ubuntu来源失败：${error.message}`;
        showToast('添加Ubuntu来源失败: ' + error.message, 'error');
        addLogEntry('添加Ubuntu ADB来源失败: ' + error.message, 'error');
    } finally {
        if (passwordInput) passwordInput.value = '';
        adbProxyOperationRunning = false;
        button.disabled = false;
        button.textContent = '安装并添加';
        updateAdbProxyButton();
        renderAdbProxyAssignments();
        renderAdbProxySourceDevices();
        if (finalMessage) message.textContent = finalMessage;
    }
}

function renderAdbProxyHosts(selection = null) {
    const sourceSelect = document.getElementById('adb-proxy-source-host');
    const targetSelect = document.getElementById('adb-proxy-target-host');
    const message = document.getElementById('adb-proxy-message');
    if (!sourceSelect || !targetSelect || !adbProxyStatus) return;
    const previousSource = selection?.sourceWorkerId || sourceSelect.value;
    const hosts = adbProxyStatus.hosts || [];
    const activeAssignments = adbProxyStatus.assignments || [];
    const activeTargets = new Set(
        activeAssignments.map(item => item.target_worker_id)
    );
    const sourceCapable = hosts.filter(host => (
        host.adb_proxy && ['online', 'busy'].includes(host.status)
    ));
    const localWorkerId = adbProxyStatus.local_worker_id || workspaceLocalWorkerId();
    // Keep an online source selectable while its last device is unplugged, so
    // the open modal can show the device again as soon as a heartbeat reports
    // the hotplug event. Targets that currently aggregate a source remain
    // excluded from becoming sources themselves.
    const sourceHosts = sourceCapable.filter(host => (
        !activeTargets.has(host.worker_id)
        && host.worker_id !== localWorkerId
    ));
    sourceSelect.replaceChildren();
    sourceHosts.forEach(host => sourceSelect.append(
        new Option(adbProxyHostLabel(host), host.worker_id)
    ));
    if (!sourceHosts.length) {
        sourceSelect.append(new Option('没有可用的ADB设备来源', ''));
    } else if (sourceHosts.some(host => host.worker_id === previousSource)) {
        sourceSelect.value = previousSource;
    }

    renderAdbProxySourceDevices(selection);
    if (!adbProxyStatus.cluster_enabled && sourceCapable.length < 2) {
        message.textContent = (
            '单机模式下本机ADB设备已直接可用，无需再次接入。若设备连接在另一台Ubuntu主机，'
            + '请部署Worker并启用集群模式。'
        );
    }
}

function adbProxyTargetUnavailableReason(host, activeSources) {
    if (!host.adb_proxy) return 'ADB Proxy未安装或版本不兼容';
    if (host.adb_proxy_source_only) return '仅可作为设备来源';
    if (activeSources.has(host.worker_id)) return '正在作为设备来源';
    if (['busy', 'draining'].includes(host.status)) return '测试中，不可用';
    if (host.status !== 'online') return '离线，不可用';
    return '';
}

function renderAdbProxySourceDevices(selection = null) {
    const sourceId = document.getElementById('adb-proxy-source-host')?.value || '';
    const targetSelect = document.getElementById('adb-proxy-target-host');
    const deviceSelect = document.getElementById('adb-proxy-source-devices');
    const submit = document.getElementById('adb-proxy-connect-submit');
    const message = document.getElementById('adb-proxy-message');
    if (!targetSelect || !deviceSelect || !submit || !message) return;
    const assignments = adbProxyStatus?.assignments || [];
    const existingAssignment = assignments.find(
        item => item.source_worker_id === sourceId
    );
    const activeSources = new Set(
        assignments.map(item => item.source_worker_id)
    );
    const hosts = adbProxyStatus?.hosts || [];
    const host = hosts.find(item => item.worker_id === sourceId);
    const previousTarget = selection?.targetWorkerId || targetSelect.value;
    const targetCandidates = existingAssignment
        ? hosts.filter(item => item.worker_id === existingAssignment.target_worker_id)
        : hosts.filter(item => item.worker_id !== sourceId);
    const targetOptions = targetCandidates.map(item => ({
        host: item,
        reason: adbProxyTargetUnavailableReason(item, activeSources)
    }));
    const targetHosts = targetOptions.filter(item => !item.reason).map(item => item.host);
    const unavailableTargets = targetOptions.filter(item => item.reason);
    targetSelect.replaceChildren();
    targetHosts.forEach(item => targetSelect.append(
        new Option(adbProxyHostLabel(item), item.worker_id)
    ));
    if (!targetHosts.length) {
        const placeholder = new Option('没有可用的ADB接入主机', '', true, true);
        placeholder.disabled = true;
        targetSelect.append(placeholder);
    } else {
        const preferred = existingAssignment?.target_worker_id
            || (targetHosts.some(item => item.worker_id === previousTarget)
                ? previousTarget
                : workspaceWorkerId());
        if (targetHosts.some(item => item.worker_id === preferred)) {
            targetSelect.value = preferred;
        }
    }
    unavailableTargets.forEach(({host: item, reason}) => {
        const option = new Option(
            `${adbProxyHostLabel(item)}（${reason}）`,
            item.worker_id
        );
        option.disabled = true;
        option.title = reason;
        targetSelect.append(option);
    });
    deviceSelect.replaceChildren();
    const assigned = new Set(existingAssignment?.devices || []);
    const devices = (host?.devices || []).filter(device => (
        device.state === 'available'
        && device.transport !== 'adb_proxy'
        && !assigned.has(device.serial)
    ));
    devices.forEach(device => {
        const detail = [device.model, device.transport].filter(Boolean).join(' · ');
        const option = new Option(
            `${device.serial}${detail ? ` · ${detail}` : ''}`,
            device.serial
        );
        option.selected = selection?.knownDeviceSerials?.has(device.serial)
            ? selection.selectedDeviceSerials.has(device.serial)
            : true;
        deviceSelect.append(option);
    });
    if (!devices.length) {
        deviceSelect.append(new Option('该来源没有可接入的ADB设备', ''));
    }
    deviceSelect.disabled = !devices.length;
    submit.disabled = (
        adbProxyOperationRunning || !sourceId || !targetSelect.value || !devices.length
    );
    if (adbProxyOperationRunning) {
        message.textContent = '正在更新ADB接入，请稍候...';
    } else if (sourceId && existingAssignment && devices.length && targetHosts.length) {
        message.textContent = (
            `该来源还有 ${devices.length} 台ADB设备可追加接入 `
            + `${existingAssignment.target_worker_id}。`
        );
    } else if (sourceId && devices.length && targetHosts.length) {
        message.textContent = `请选择要接入的ADB设备，共 ${devices.length} 台可用。`;
    } else if (
        sourceId && devices.length
        && unavailableTargets.some(item => item.reason === '测试中，不可用')
    ) {
        const busyHosts = unavailableTargets
            .filter(item => item.reason === '测试中，不可用')
            .map(item => adbProxyHostLabel(item.host))
            .join('、');
        message.textContent = `${busyHosts} 正在执行测试，暂不能作为ADB接入主机。`;
    } else if (sourceId && devices.length && unavailableTargets.length) {
        message.textContent = '接入主机当前不可用，请在下拉框中查看原因。';
    } else if (assignments.length) {
        message.textContent = '当前没有剩余可接入的ADB设备；已有接入可在上方查看或断开。';
    } else if (!sourceId) {
        message.textContent = '没有可用的ADB设备来源。';
    } else if (!targetHosts.length) {
        message.textContent = '没有可用于接入该来源设备的目标主机。';
    } else {
        message.textContent = '该来源当前没有可接入的ADB设备。';
    }
}

async function refreshAdbProxyAssignments() {
    const container = document.getElementById('adb-proxy-assignments');
    const hadRenderedAssignments = container?.dataset.loaded === 'true';
    if (container) {
        container.setAttribute('aria-busy', 'true');
        if (!hadRenderedAssignments) container.textContent = '正在刷新接入状态...';
    }
    try {
        const selection = adbProxySelectionSnapshot();
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        renderAdbProxyAssignments();
        renderAdbProxyHosts(selection);
    } catch (error) {
        if (container && !hadRenderedAssignments) container.textContent = `刷新失败: ${error.message}`;
        else showToast(`ADB接入状态刷新失败: ${error.message}`, 'error');
    } finally {
        if (container) container.setAttribute('aria-busy', 'false');
    }
}

function renderAdbProxyAssignments() {
    const container = document.getElementById('adb-proxy-assignments');
    if (!container) return;
    container.replaceChildren();
    const assignments = adbProxyStatus?.assignments || [];
    if (!assignments.length) {
        container.textContent = '当前没有通过adbproxy-rs接入的设备。';
        container.dataset.loaded = 'true';
        return;
    }
    assignments.forEach(assignment => {
        const row = document.createElement('div');
        row.className = 'adb-proxy-assignment';
        if (['connected', 'connecting', 'connect_failed', 'disconnect_failed', 'host_offline',
            'recovering', 'degraded_source', 'degraded_target', 'device_missing'].includes(assignment.status)) {
            row.classList.add(`routing-status-${assignment.status}`);
        }
        const info = document.createElement('div');
        info.className = 'adb-proxy-assignment-info';
        const statusLabels = {
            connected: '已接入',
            connecting: '正在接入',
            connect_failed: '接入失败',
            disconnect_failed: '断开失败，需重试',
            host_offline: '主机离线',
            recovering: '正在核对',
            degraded_source: '来源代理异常',
            degraded_target: '目标Hub异常',
            device_missing: '目标设备缺失',
        };
        const status = statusLabels[assignment.status] || '';
        info.textContent = (
            `${assignment.source_worker_id} → ${assignment.target_worker_id}`
            + `｜设备：${(assignment.devices || []).join(', ') || '无'}`
            + (status ? `｜${status}` : '')
        );
        const actions = document.createElement('div');
        actions.className = 'device-routing-actions';
        const canInspectFailure = [
            'connect_failed',
            'disconnect_failed',
            'host_offline',
            'degraded_source',
            'degraded_target',
            'device_missing',
        ].includes(assignment.status);
        if (canInspectFailure) {
            const inspectFailure = document.createElement('button');
            inspectFailure.type = 'button';
            inspectFailure.className = 'btn-xxs';
            inspectFailure.textContent = '查看原因';
            inspectFailure.addEventListener('click', () => showAdbProxyDiagnostics(assignment));
            actions.append(inspectFailure);
        }
        const disconnect = document.createElement('button');
        disconnect.type = 'button';
        disconnect.className = 'btn-xxs btn-danger';
        disconnect.textContent = '断开';
        disconnect.disabled = adbProxyOperationRunning;
        disconnect.addEventListener('click', () => disconnectAdbProxyAssignment(
            assignment.source_worker_id,
            assignment.target_worker_id
        ));
        actions.append(disconnect);
        row.append(info, actions);
        container.append(row);
    });
    container.dataset.loaded = 'true';
}

async function showAdbProxyDiagnostics(assignment) {
    const {modal, modalId} = createAnalysisModal(
        'adb-proxy-diagnostics',
        'ADB Proxy 诊断',
        '正在读取双端状态和最近日志...'
    );
    // 须在首个 await 前注册：加载期间关闭时 close() 才能移除弹窗节点。
    ModalManager.onClose(modalId, () => modal.remove());
    try {
        const workers = Array.from(new Set([
            assignment.source_worker_id,
            assignment.target_worker_id,
        ].filter(Boolean)));
        const logs = await Promise.all(workers.map(workerId => apiCall(
            '/api/adb-forward/logs?worker_id=' + encodeURIComponent(workerId),
            'GET'
        )));
        const body = modal.querySelector('.modal-body');
        body.replaceChildren();
        const status = document.createElement('pre');
        status.className = 'transport-diagnostics-output';
        status.textContent = JSON.stringify({
            status: assignment.status,
            generation: assignment.generation || 0,
            health: assignment.health || {},
        }, null, 2);
        body.append(status);
        logs.forEach(item => {
            const heading = document.createElement('h4');
            heading.textContent = item.worker_id;
            const output = document.createElement('pre');
            output.className = 'transport-diagnostics-output';
            output.textContent = [
                ...(item.notice ? [`说明：${item.notice}`] : []),
                '--- proxy.log ---', ...(item.proxy || []),
                '--- hub.log ---', ...(item.hub || []),
            ].join('\n');
            body.append(heading, output);
        });
    } catch (error) {
        showModalError(modal, error.message);
    }
}

async function submitAdbProxyConnect() {
    const sourceWorkerId = document.getElementById('adb-proxy-source-host')?.value || '';
    const targetWorkerId = document.getElementById('adb-proxy-target-host')?.value || '';
    const devices = Array.from(
        document.getElementById('adb-proxy-source-devices')?.selectedOptions || []
    ).map(option => option.value).filter(Boolean);
    if (!sourceWorkerId || !targetWorkerId || !devices.length) {
        showToast('请选择设备来源、接入主机和至少一台ADB设备', 'warning');
        return;
    }
    await runAdbProxyOperation(async () => {
        const result = await apiCall('/api/adb-forward/start', 'POST', {
            source_worker_id: sourceWorkerId,
            target_worker_id: targetWorkerId,
            devices
        });
        addLogEntry(result.message || 'ADB设备接入完成', 'success');
        return result;
    });
}

async function disconnectAdbProxyAssignment(sourceWorkerId, targetWorkerId) {
    await runAdbProxyOperation(async () => {
        const result = await apiCall('/api/adb-forward/stop', 'POST', {
            source_worker_id: sourceWorkerId,
            target_worker_id: targetWorkerId
        });
        addLogEntry(result.message || 'ADB设备接入已断开', 'success');
        return result;
    });
}

async function refreshAdbProxyTargetDevices(result) {
    const targetWorkerId = result?.assignment?.target_worker_id
        || result?.target_worker_id
        || '';
    if (!targetWorkerId || targetWorkerId !== workspaceWorkerId()) return;
    try {
        await loadDevices(true, {silent: true});
        addWorkerLog(targetWorkerId, `已自动刷新 ${targetWorkerId} 的ADB设备列表`, 'info');
    } catch (error) {
        addLogEntry(`ADB接入已更新，但设备列表自动刷新失败: ${error.message}`, 'warning');
    }
}

async function runAdbProxyOperation(operation) {
    const message = document.getElementById('adb-proxy-message');
    let operationError = '';
    adbProxyOperationRunning = true;
    updateAdbProxyButton();
    renderAdbProxyAssignments();
    renderAdbProxySourceDevices();
    if (message) message.textContent = '正在更新ADB接入，请稍候...';
    try {
        const result = await operation();
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        state.adbForwardRunning = Boolean(adbProxyStatus.connected);
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        await refreshAdbProxyTargetDevices(result);
    } catch (error) {
        operationError = `ADB接入操作失败：${error.message}`;
        addLogEntry('ADB接入操作失败: ' + error.message, 'error');
        showToast('ADB接入操作失败: ' + error.message, 'error');
    } finally {
        adbProxyOperationRunning = false;
        updateAdbProxyButton();
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        if (operationError && message) message.textContent = operationError;
    }
}

function updateAdbProxyButton() {
    const button = document.getElementById('adb-forward-btn');
    if (!button) return;
    button.disabled = adbProxyOperationRunning;
    button.textContent = state.adbForwardRunning
        ? '🔌 管理ADB'
        : '🔌 ADB接入';
}

