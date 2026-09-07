// Cluster workspace, device inventory, and suite selection extracted from navigation.js.
// ==================== Device Management ====================
let _loadClusterWorkersInFlight = null;
async function loadClusterWorkers(forceRefresh = false) {
    const select = document.getElementById('cluster-worker');
    if (!select) return;
    if (typeof initializeClusterMode === 'function') {
        await initializeClusterMode();
    }
    if (_loadClusterWorkersInFlight) return _loadClusterWorkersInFlight;
    _loadClusterWorkersInFlight = (async () => {
        try {
            await (window.GmsWorkspace?.ready || Promise.resolve());
            const localWorkerId = workspaceLocalWorkerId();
            const workers = (await window.GmsWorkspace.loadClusterWorkers(forceRefresh)).filter(worker =>
                worker.status !== 'offline'
                && (state.clusterStatus?.enabled || worker.id === localWorkerId)
            );
            const context = window.GmsWorkspace?.get?.() || {};
            const optionData = [];
            if (!workers.some(worker => worker.id === localWorkerId)) {
                optionData.push({value: localWorkerId, label: localWorkerId});
            }
            for (const worker of workers) {
                optionData.push({
                    value: worker.id,
                    label: worker.id,
                });
            }

            const availableValues = new Set(optionData.map(option => option.value));
            const contextWorkerId = context.worker_id || localWorkerId;
            const selectedWorkerId = availableValues.has(contextWorkerId)
                ? contextWorkerId
                : (availableValues.has(select.value)
                    ? select.value
                    : (availableValues.has(localWorkerId) ? localWorkerId : optionData[0]?.value || ''));
            const optionsUnchanged = select.options.length === optionData.length
                && optionData.every((option, index) => {
                    const currentOption = select.options[index];
                    return currentOption.value === option.value
                        && currentOption.textContent === option.label;
                });

            // 列表没有变化时保留现有 DOM，避免自动刷新导致原生 select 重新绘制。
            if (!optionsUnchanged) {
                const fragment = document.createDocumentFragment();
                for (const item of optionData) {
                    const option = document.createElement('option');
                    option.value = item.value;
                    option.textContent = item.label;
                    option.selected = item.value === selectedWorkerId;
                    fragment.appendChild(option);
                }
                select.replaceChildren(fragment);
            } else if (select.value !== selectedWorkerId) {
                select.value = selectedWorkerId;
            }

            select.dataset.workersLoaded = 'true';
            select.setAttribute('aria-busy', 'false');
            const clusterModeEnabled = Boolean(
                state.clusterStatus?.enabled && context.scope_mode === 'cluster'
            );
            select.disabled = !clusterModeEnabled;
        } finally {
            _loadClusterWorkersInFlight = null;
        }
    })();
    return _loadClusterWorkersInFlight;
}

async function resolveClusterHost(workerId) {
    let hosts;
    if (typeof window.loadClusterHostDirectory === 'function') {
        hosts = await window.loadClusterHostDirectory();
    } else {
        const response = await fetch('/api/cluster/hosts', {cache: 'no-store'});
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.success === false) {
            throw new Error(payload.error || `Worker ${workerId} 主机信息加载失败`);
        }
        hosts = payload.hosts || [];
    }
    const host = hosts.find(item => item.worker_id === workerId);
    if (!host || !host.address || !host.ssh_user) {
        throw new Error(`Worker ${workerId} 缺少 SSH 主机信息`);
    }
    if (host.status === 'offline') {
        throw new Error(`Worker ${workerId} 当前离线`);
    }
    return host;
}

function updateTestHostScopedControls(workerId = workspaceWorkerId()) {
    const remoteSelected = Boolean(workerId && !isLocalWorkspaceWorker(workerId));
    // 诊断类按钮（SSHD/路由）作用于平台网络（Controller/设备源主机），
    // 与所选测试 Worker 无关；VPN 则按当前测试主机路由。
    // 远端 Worker 模式下均保持可点击，仅提示作用域。
    const controllerScoped = {
        'check-sshd-btn': 'SSHD 检查面向设备源主机（Controller 执行）',
        'check-routing-btn': '路由检查面向 Controller 与浏览器客户端',
        'vpn-connect-btn': 'VPN 连接由当前选中的测试主机管理',
    };
    Object.entries(controllerScoped).forEach(([id, message]) => {
        const control = document.getElementById(id);
        if (!control) return;
        control.disabled = false;
        control.title = remoteSelected
            ? `${message}；当前已选择远端 Worker`
            : '';
    });
    const usbip = document.getElementById('usbip-btn');
    if (usbip) {
        usbip.disabled = false;
        usbip.title = remoteSelected
            ? '选择设备来源和接入主机；远端 Worker 接入将在后端分发启用后开放'
            : '选择设备来源、接入主机和 USB 设备';
    }
    const adbForward = document.getElementById('adb-forward-btn');
    if (adbForward) {
        adbForward.disabled = adbProxyOperationRunning;
        adbForward.title = '选择ADB设备来源和接入主机（不依赖USB/IP内核模块）';
    }
}

function workspaceLocalWorkerId() {
    return window.GmsWorkspace?.localWorkerId?.()
        || state.clusterStatus?.local_worker_id
        || 'ats-worker-controller';
}

function isLocalWorkspaceWorker(workerId) {
    // 只信 workspace context 同步的真实 local_worker_id；
    // 'ats-worker-controller' 保留为 Cluster Status 尚未加载时的 fallback，
    // 不再作为独立判断条件，避免自定义 local_worker_id 时误判双本机。
    return !workerId || workerId === workspaceLocalWorkerId();
}

function workspaceWorkerId() {
    const context = window.GmsWorkspace?.get?.() || {};
    if (!state.clusterStatus?.enabled || context.scope_mode !== 'cluster') return workspaceLocalWorkerId();
    return context.worker_id || workspaceLocalWorkerId();
}

function syncWorkspaceWorkerSelectors(workerId) {
    // 只同步执行目标选择器（cluster-worker）与 Suite Browser 的浏览目标。
    // reports-worker-filter 是查询条件，属于 Reports 页本地状态，
    // 不跟随 Test Worker 联动（否则切测试主机会重写报告筛选）。
    for (const id of ['cluster-worker', 'suite-worker-select']) {
        const select = document.getElementById(id);
        if (!select) continue;
        if (Array.from(select.options).some(option => option.value === workerId)) {
            select.value = workerId;
        }
    }
}

function updateClusterToggleUI(enabled) {
    const row = document.getElementById('cluster-mode-toggle-row');
    const label = document.getElementById('cluster-mode-label');
    if (!row || !label) return;
    if (enabled) {
        label.textContent = '集群模式 · 点击切换单机';
        row.title = '当前为集群模式，点击切换到单机模式';
        row.classList.add('active');
    } else {
        label.textContent = '单机模式 · 点击切换集群';
        row.title = '当前为单机模式，点击切换到集群模式';
        row.classList.remove('active');
    }
}

function applyClusterMode(enabled) {
    const body = document.body;
    if (body) {
        body.classList.remove(
            'workspace-scope-pending',
            'workspace-scope-single',
            'workspace-scope-cluster'
        );
        body.classList.add(enabled ? 'workspace-scope-cluster' : 'workspace-scope-single');
    }
    document.querySelectorAll('.sidebar-item[data-page="cluster"]').forEach(item => {
        // 集群管理页也是启用集群模式和接入 Worker 的入口，单机模式下也必须可见。
        item.style.display = '';
    });
    const testHostSelect = document.getElementById('cluster-worker');
    if (testHostSelect) {
        const workersLoaded = testHostSelect.dataset.workersLoaded === 'true';
        testHostSelect.disabled = !enabled || !workersLoaded;
        testHostSelect.title = enabled
            ? (workersLoaded
                ? '选择执行测试的 Cluster Worker'
                : '正在加载测试主机列表')
            : '当前为单机模式；切换到集群模式后可选择远端测试主机';
    }
    const suiteWorkerSelect = document.getElementById('suite-worker-select');
    if (suiteWorkerSelect?.closest('label')) suiteWorkerSelect.closest('label').style.display = enabled ? '' : 'none';
    const reportsWorkerSelect = document.getElementById('reports-worker-filter');
    const reportsHostFilter = reportsWorkerSelect?.closest('label');
    if (reportsHostFilter) {
        reportsHostFilter.style.visibility = enabled ? 'visible' : 'hidden';
        reportsHostFilter.style.pointerEvents = enabled ? '' : 'none';
    }
    const terminalControl = document.getElementById('terminal-worker-control');
    if (terminalControl) terminalControl.style.display = enabled ? 'contents' : 'none';
    if (!enabled) {
        syncWorkspaceWorkerSelectors(workspaceLocalWorkerId());
    }
    if (typeof window.applyHostWorkspaceScopeMode === 'function') {
        window.applyHostWorkspaceScopeMode(enabled);
    }
    updateTestHostScopedControls(enabled
        ? (window.GmsWorkspace?.get?.().worker_id || workspaceLocalWorkerId())
        : workspaceLocalWorkerId());
    updateClusterToggleUI(enabled);
}

async function toggleClusterMode() {
    const row = document.getElementById('cluster-mode-toggle-row');
    if (row) { row.style.pointerEvents = 'none'; row.style.opacity = '0.6'; }
    try {
        const infrastructureEnabled = Boolean(state.clusterStatus?.enabled);
        const wasEnabled = infrastructureEnabled && window.GmsWorkspace?.get?.().scope_mode === 'cluster';
        if (!wasEnabled && !infrastructureEnabled) {
            throw new Error('集群基础设施未启用，请先在服务端 configs/cluster.json 启用集群能力');
        }
        const context = window.GmsWorkspace?.update({
            scope_mode: wasEnabled ? 'single' : 'cluster',
            worker_id: wasEnabled ? workspaceLocalWorkerId() : (window.GmsWorkspace?.get?.().worker_id || workspaceLocalWorkerId()),
            device_ids: []
        }, {source: 'cluster-toggle'});
        const enabled = context?.scope_mode === 'cluster';
        applyClusterMode(enabled);
        if (enabled) {
            await loadClusterWorkers().catch(error => debugLog('[Cluster] Worker list unavailable:', error));
            showToast('已切换到集群模式', 'success');
        } else {
            showToast('已切换到单机模式', 'success');
        }
        // 重新加载设备列表以反映模式变化
        await Promise.all([loadDevices(true), loadTestSuites(true)]);
        // 模式切换时同步刷新报告列表的 Worker 过滤器
        if (typeof loadTestReports === 'function') {
            loadTestReports(currentUserFilter, false, true).catch(() => {});
        }

    } catch (error) {
        showToast(`切换模式失败: ${error.message}`, 'error');
    } finally {
        if (row) { row.style.pointerEvents = ''; row.style.opacity = ''; }
    }
}

window.toggleClusterMode = toggleClusterMode;

let _initializeClusterModeInFlight = null;
let _clusterModeInitialized = false;
async function initializeClusterMode() {
    if (_clusterModeInitialized) {
        const context = window.GmsWorkspace?.get?.() || {};
        return Boolean(state.clusterStatus?.enabled && context.scope_mode === 'cluster');
    }
    if (_initializeClusterModeInFlight) return _initializeClusterModeInFlight;
    _initializeClusterModeInFlight = (async () => {
        try {
            const [status, context] = await Promise.all([
                window.GmsWorkspace.loadClusterStatus(),
                window.GmsWorkspace?.ready || Promise.resolve({scope_mode: 'single', worker_id: workspaceLocalWorkerId()})
            ]);
            state.clusterStatus = status;
            const enabled = Boolean(status.enabled && context.scope_mode === 'cluster');
            applyClusterMode(enabled);
            if (!enabled && context.scope_mode === 'cluster') {
                window.GmsWorkspace?.update({scope_mode: 'single', worker_id: workspaceLocalWorkerId(), device_ids: []},
                    {source: 'cluster-unavailable'});
            }
            return enabled;
        } catch (error) {
            debugLog('[Cluster] Status unavailable, preserving single-host UI:', error);
            applyClusterMode(false);
            return false;
        } finally {
            _clusterModeInitialized = true;
            _initializeClusterModeInFlight = null;
        }
    })();
    return _initializeClusterModeInFlight;
}

window.addEventListener('gms:workspace-context', event => {
    const context = event.detail?.context || {};
    const previous = event.detail?.previous || {};
    const enabled = Boolean(state.clusterStatus?.enabled && context.scope_mode === 'cluster');
    const workerId = enabled ? (context.worker_id || workspaceLocalWorkerId()) : workspaceLocalWorkerId();
    applyClusterMode(enabled);
    syncWorkspaceWorkerSelectors(workerId);
    updateTestHostScopedControls(workerId);
    if (previous.scope_mode !== context.scope_mode
            && typeof currentPage !== 'undefined' && currentPage === 'devices'
            && typeof loadDevicesManagement === 'function') {
        // 模式切换后立即刷新对应设备清单。
        setTimeout(() => loadDevicesManagement().catch(error =>
            debugLog('[Devices] Scope refresh failed:', error)), 0);
    }
    const contextJobId = String(context.cluster_job_id || '');
    const contextSource = String(event.detail?.source || '');
    const reportProvenanceOnly = ['reports', 'report-analysis', 'report-download', 'test-suites', 'automation']
        .includes(String(context.origin_page || ''))
        || ['reports', 'report-analysis'].includes(contextSource);
    if (contextJobId && contextJobId !== state.clusterJobId && !reportProvenanceOnly) {
        state.clusterJobId = contextJobId;
        resetClusterEventCursor();
        sessionStorage.setItem('active_cluster_job', contextJobId);
        wakeTestStatusPolling();
    }

    const previousWorker = previous.scope_mode === 'cluster'
        ? (previous.worker_id || workspaceLocalWorkerId())
        : workspaceLocalWorkerId();
    if (previousWorker !== workerId) {
        state.selectedDevices.clear();
        testSuitesCache = [];
        testSuitesWorkerId = '';
        renderTestSuitesDropdown();
    }
});

let testWorkerSwitchGeneration = 0;

async function switchTestWorker() {
    const workerId = document.getElementById('cluster-worker')?.value || workspaceLocalWorkerId();
    const switchGeneration = ++testWorkerSwitchGeneration;
    const previousWorkerLabel = isLocalWorkspaceWorker(workspaceWorkerId())
        ? '本机测试主机'
        : (window.GmsWorkspace?.get?.().worker_id || '上一台主机');
    const interruptedJobId = state.testing && state.clusterJobId ? state.clusterJobId : '';
    state.selectedDevices.clear();
    window.GmsWorkspace?.update({
        scope_mode: isLocalWorkspaceWorker(workerId) ? window.GmsWorkspace.get().scope_mode : 'cluster',
        worker_id: workerId,
        device_ids: [],
        cluster_job_id: '',
        attempt_id: ''
    }, {source: 'test'});
    syncWorkspaceWorkerSelectors(workerId);
    updateTestHostScopedControls(workerId);
    // 立即清除旧主机的测试状态：内存、sessionStorage、workspace 持久层
    // 三处同步清理，否则 F5 后 active_cluster_job 会把旧 Worker 的 job
    // 恢复到新主机上下文。旧 job 继续在后端跑；refreshTestStatusForWorker
    // 查询到新主机有活跃测试时再绑定新 job。
    state.clusterJobId = '';
    resetClusterEventCursor();
    state.testing = false;
    state.testStopping = false;
    sessionStorage.removeItem('active_cluster_job');
    updateTestToggleButton(false);
    // 切主机后按新的 worker scope 重过滤日志面板，
    // 其他 Worker 的历史保留在 DOM 中（隐藏而非删除）。
    if (typeof applyLogScopeFilter === 'function') {
        applyLogScopeFilter();
    }
    refreshTestStatusForWorker(workerId);
    try {
        testSuitesCache = [];
        testSuitesWorkerId = '';
        await Promise.all([loadDevices(true), loadTestSuites(true)]);
        if (
            switchGeneration !== testWorkerSwitchGeneration
            || workspaceWorkerId() !== workerId
        ) return;
        // 日志面板是会话级、跨主机共用的：写一条切换标记，
        // 避免不同主机的操作/测试日志混在一起无法分辨。
        addLogEntry(`已切换测试主机: ${isLocalWorkspaceWorker(workerId) ? '本机测试主机' : workerId}`, 'info');
        if (interruptedJobId) {
            addLogEntry(
                `${previousWorkerLabel} 上的测试仍在后台运行，测试日志已停止滚动；完成后可在测试报告中查看结果`,
                'info'
            );
        }
        showToast(isLocalWorkspaceWorker(workerId)
            ? '已切换到本机测试主机'
            : `已切换到 ${workerId}，发现 ${state.devices.length} 台设备`, 'success');
    } catch (error) {
        showToast(`切换 Worker 失败: ${error.message}`, 'error');
    }
}

window.switchTestWorker = switchTestWorker;

let _refreshTestStatusGeneration = 0;
async function refreshTestStatusForWorker(workerId) {
    const generation = ++_refreshTestStatusGeneration;
    try {
        const status = await apiCall('/api/test/status?logs=false');
        // 丢弃过期响应：用户可能又切换到了另一台主机
        if (generation !== _refreshTestStatusGeneration) return;
        const activeJobs = Array.isArray(status.active_jobs) ? status.active_jobs : [];
        const job = activeJobs.find(j => j.worker_id === workerId);
        if (job) {
            state.testing = true;
            state.clusterJobId = job.id;
            resetClusterEventCursor();
            state.testStopping = job.status === 'stopping';
            sessionStorage.setItem('active_cluster_job', job.id);
            window.GmsWorkspace?.update(
                {cluster_job_id: job.id, attempt_id: job.attempt_id || ''},
                {source: 'worker-switch'}
            );
            updateTestToggleButton(true);
            wakeTestStatusPolling();
        } else {
            // 当前主机没有活跃测试，恢复空闲状态并清掉持久层的旧 job 绑定
            // （否则 F5 后 active_cluster_job 会恢复别的 Worker 的任务）。
            state.testing = false;
            state.testStopping = false;
            state.clusterJobId = '';
            resetClusterEventCursor();
            sessionStorage.removeItem('active_cluster_job');
            window.GmsWorkspace?.update(
                {cluster_job_id: '', attempt_id: ''},
                {source: 'worker-switch-idle'}
            );
            updateTestToggleButton(false);
        }
    } catch (error) {
        debugLog('[Worker Switch] Failed to check test status:', error);
        // 请求失败时保守地保持当前状态不变，避免误清测试状态
    }
}

let deviceRefreshGeneration = 0;
const deviceRefreshFlights = new Map();

function isSelectableTestDevice(device) {
    if (typeof device === 'string') return true;
    const status = device.status || device.state || 'online';
    return !device.locked && ['online', 'available'].includes(status);
}

function isSelectableBootloaderDevice(device) {
    if (typeof device === 'string') return true;
    if (device.transport === 'adb_proxy') return false;
    const status = device.status || device.state || 'online';
    if (status === 'fastboot') {
        return !device.locked && !device.cluster_worker_id;
    }
    return !device.locked && ['online', 'available'].includes(status);
}

function isSelectableWorkspaceDevice(device) {
    if (typeof device === 'string') return true;
    const status = device.status || device.state || 'online';
    if (status === 'fastboot') {
        // 当前 GSI 直刷 Fastbootd 仅由本机 /api/burn/gsi 支持；
        // 远端 Worker 仍需从 available 状态发起。
        return !device.locked && !device.cluster_worker_id;
    }
    return !device.locked && ['online', 'available'].includes(status);
}

function isSelectableRebootDevice(device) {
    if (isSelectableTestDevice(device)) return true;
    // 停在 Fastboot/Fastbootd 的设备也支持重启：本机走 fastboot reboot；
    // 远端 Worker 设备仍需回到 ADB 状态后发起。
    const status = device.status || device.state || 'online';
    return status === 'fastboot' && !device.locked && !device.cluster_worker_id;
}

function selectedDeviceIdsMatching(predicate) {
    return Array.from(state.selectedDevices).filter(deviceId => {
        const device = state.devices.find(item => (
            (typeof item === 'string' ? item : item.device_id) === deviceId
        ));
        return device && predicate(device);
    });
}

function selectedTestDeviceIds() {
    return selectedDeviceIdsMatching(isSelectableTestDevice);
}

function selectedWorkspaceDeviceIds() {
    return selectedDeviceIdsMatching(isSelectableWorkspaceDevice);
}

function fetchDevicesForWorker(workerId, forceRefresh, source) {
    const requestKey = `${workerId}\n${forceRefresh ? 'force' : 'cached'}`;
    const existing = deviceRefreshFlights.get(requestKey);
    if (existing) return existing;

    const request = (async () => {
        if (isLocalWorkspaceWorker(workerId)) {
            const params = new URLSearchParams();
            if (forceRefresh) params.set('force_refresh', '1');
            if (source) params.set('source', source);
            const query = params.toString();
            const url = `/api/devices/list${query ? '?' + query : ''}`;
            return apiCall(url);
        }
        const response = await fetch(`/api/cluster/devices?worker_id=${encodeURIComponent(workerId)}`, {cache: 'no-store'});
        if (!response.ok) throw new Error(`Worker ${workerId} 设备加载失败 (HTTP ${response.status})`);
        const payload = await response.json();
        return (payload.devices || [])
            .filter(device => !['offline', 'unknown'].includes(device.state))
            .map(device => {
                const lockedStates = ['allocated', 'leased', 'busy', 'external_busy', 'reserved'];
                const isLocked = Boolean(device.claimed || lockedStates.includes(device.state));
                const lockLabels = {
                    'cluster-job': '测试任务',
                    'cluster-device-action': '设备操作',
                    'cluster-firmware': '固件烧写'
                };
                return {
                    ...(device.properties || {}),
                    device_id: device.id,
                    serial: device.serial,
                    worker_id: workerId,
                    cluster_worker_id: workerId,
                    transport: device.transport || 'local_usb',
                    locked: isLocked,
                    locked_by: isLocked
                        ? (lockLabels[device.claim_source_type]
                            || device.claim_source_type
                            || device.state
                            || '平台操作')
                        : '',
                    locked_by_self: false,
                    state: device.state,
                    status: device.state === 'available' ? 'online' : device.state
                };
            });
    })();
    deviceRefreshFlights.set(requestKey, request);
    const clearFlight = () => {
        if (deviceRefreshFlights.get(requestKey) === request) {
            deviceRefreshFlights.delete(requestKey);
        }
    };
    request.then(clearFlight, clearFlight);
    return request;
}

async function loadDevices(forceRefresh = false, options = {}) {
    const workerId = workspaceWorkerId();
    const generation = ++deviceRefreshGeneration;
    const silent = Boolean(options && options.silent);
    const source = (options && options.source) || 'auto';
    state.isRefreshingDevices = true;
    state.deviceRefreshPromise = fetchDevicesForWorker(workerId, forceRefresh, source);

    try {
        const devices = await state.deviceRefreshPromise;
        // 丢弃非当前 Worker 的延迟响应。
        if (generation !== deviceRefreshGeneration || workspaceWorkerId() !== workerId) {
            return state.devices;
        }
        state.devices = devices;
        // 设备列表更新后，只清理选中集合里已消失的设备。
        // 已不可选（占用/状态异常）的设备保留在选中集合和列表展示中：
        // 复选框由渲染层按"可选才显示勾选"处理，设备恢复可选后勾选自动恢复。
        const currentIds = new Set(devices.map(d => typeof d === 'string' ? d : d.device_id).filter(Boolean));
        for (const id of Array.from(state.selectedDevices)) {
            if (!currentIds.has(id)) {
                state.selectedDevices.delete(id);
            }
        }
        for (const id of window.GmsWorkspace?.get?.().device_ids || []) {
            if (currentIds.has(id)) state.selectedDevices.add(id);
        }
        // 先用已有数据渲染设备，让 ADB 区立刻显示，不被分组加载阻塞
        renderDevices();
        // 首次加载时并行拉取分组定义（用于按"关注"筛选），到位后再重渲染一次
        if (!state.deviceGroupsLoaded) {
            state.deviceGroupsLoaded = true;
            loadDeviceGroups()
                .then(() => renderDevices())
                .catch((err) => debugLog('[Devices] loadDeviceGroups failed:', err));
        }

        if (!silent) {
            // 显示设备信息，包含序列号和刷新来源
            const sourceLabel = source === 'manual' ? '[手动刷新] ' : '[自动刷新] ';
            let deviceInfo = `${sourceLabel}已刷新设备列表，找到 ${devices.length} 台设备`;
            if (devices.length > 0) {
                // 支持 device_id 和 serial 两种字段名
                const serials = devices.map(d => d.device_id || d.serial || '未知').filter(s => s).join(' ');
                if (serials) {
                    deviceInfo += ` (${serials})`;
                }
            }
            addWorkerLog(workerId, deviceInfo, 'info');
        }

        // 不再自动检查 USB/IP 状态，避免覆盖连接状态
        // USB/IP 状态只在连接/断开操作时更新
        return devices;
    } catch (error) {
        if (generation !== deviceRefreshGeneration || workspaceWorkerId() !== workerId) {
            return state.devices;
        }
        if (silent) {
            debugLog('[Devices] Silent refresh failed:', error.message);
        } else {
            addWorkerLog(workerId, '加载设备列表失败: ' + error.message, 'error');
        }
        throw error;
    } finally {
        if (generation === deviceRefreshGeneration) {
            state.isRefreshingDevices = false;
            state.deviceRefreshPromise = null;
        }
    }
}

function startBurnDeviceProtocolRefresh(deviceIds, options = {}) {
    const targets = new Set(
        (deviceIds || []).map(deviceId => String(deviceId || '').trim()).filter(Boolean)
    );
    const workerId = workspaceWorkerId();
    if (!targets.size || !isLocalWorkspaceWorker(workerId)) {
        return () => {};
    }

    const requestedInterval = Number(options.intervalMs);
    const requestedTimeout = Number(options.timeoutMs);
    const refreshDevices = typeof options.refreshDevices === 'function'
        ? options.refreshDevices
        : () => loadDevices(true, {silent: true});
    const intervalMs = Number.isFinite(requestedInterval) && requestedInterval > 0
        ? Math.max(10, requestedInterval)
        : 1500;
    const timeoutMs = Number.isFinite(requestedTimeout) && requestedTimeout > 0
        ? Math.max(intervalMs, requestedTimeout)
        : 45000;
    const deadline = Date.now() + timeoutMs;
    let stopped = false;
    let timer = null;
    let inFlight = false;

    const stop = () => {
        stopped = true;
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
    };
    const hasTargetFastbootDevice = devices => (devices || []).some(device => {
        const deviceId = typeof device === 'string'
            ? device
            : device.device_id || device.serial;
        const protocol = typeof device === 'string'
            ? ''
            : device.protocol || device.status || device.state;
        return targets.has(String(deviceId || ''))
            && String(protocol || '').toLowerCase() === 'fastboot';
    });
    const poll = async () => {
        if (stopped || inFlight) return;
        if (Date.now() >= deadline || workspaceWorkerId() !== workerId) {
            stop();
            return;
        }
        inFlight = true;
        try {
            const devices = await refreshDevices();
            if (hasTargetFastbootDevice(devices)) {
                debugLog('[Burn] Fastboot device became visible; stopping transition refresh');
                stop();
                return;
            }
        } catch (error) {
            debugLog('[Burn] Device transition refresh failed:', error.message);
        } finally {
            inFlight = false;
        }
    };

    timer = setInterval(() => void poll(), intervalMs);
    void poll();
    return stop;
}

// 测试套件管理
let testSuitesCache = [];
let _loadSuitesPromise = null;
let testSuitesWorkerId = '';

async function loadTestSuites(forceRefresh = false) {
    const requestedWorker = workspaceWorkerId();
    // 如果有正在进行的请求，等待它完成
    if (_loadSuitesPromise) {
        await _loadSuitesPromise;
        if (testSuitesWorkerId !== requestedWorker) return loadTestSuites(forceRefresh);
        if (forceRefresh) return loadTestSuites(true);
        return testSuitesCache;
    }

    if (!forceRefresh && testSuitesCache.length > 0 && testSuitesWorkerId === requestedWorker) {
        return testSuitesCache;
    }

    _loadSuitesPromise = (async () => {
        try {
            let response;
            if (isLocalWorkspaceWorker(requestedWorker)) {
                const url = forceRefresh ? '/api/test/suites?force_refresh=1' : '/api/test/suites';
                response = await apiCall(url);
            } else {
                // forceRefresh 时触发真正的 Worker 端套件扫描（refresh
                // 命令回写 Controller），随后读取回写后的清单——否则
                // "刚解压 suite" 点刷新仍看到旧缓存。
                if (forceRefresh) {
                    try {
                        // silentToast：refresh 失败由随后的 suites 读取
                        // 给出权威错误，后台回写失败不单独弹 toast。
                        await apiCall(
                            `/api/cluster/workers/${encodeURIComponent(requestedWorker)}/refresh?inventory=suites`,
                            'POST',
                            null,
                            {silentToast: true});
                    } catch (refreshError) {
                        debugLog(`[loadTestSuites] worker refresh failed: ${refreshError.message}`);
                    }
                }
                const remoteResponse = await fetch(`/api/cluster/suites?worker_id=${encodeURIComponent(requestedWorker)}`,
                    {cache: 'no-store'});
                if (!remoteResponse.ok) {
                    throw new Error(`Worker ${requestedWorker} 套件加载失败 (HTTP ${remoteResponse.status})`);
                }
                const payload = await remoteResponse.json();
                response = {
                    success: true,
                    count: (payload.suites || []).length,
                    suites: (payload.suites || []).filter(suite => suite.available).map(suite => ({
                        tools_path: suite.tools_path,
                        test_type: String(suite.test_type || '').toLowerCase(),
                        version: suite.version,
                        suite_key: suite.suite_key,
                        worker_id: requestedWorker
                    }))
                };
            }

            if (response.suites) {
                if (workspaceWorkerId() !== requestedWorker) return testSuitesCache;
                testSuitesCache = response.suites || [];
                testSuitesWorkerId = requestedWorker;
                renderTestSuitesDropdown();
                if (typeof renderTestSuiteBrowserList === 'function') {
                    renderTestSuiteBrowserList();
                }
                if (response.success === false) {
                    showToast(response.warning || response.error || '测试套件主机不可用', 'warning');
                    return testSuitesCache;
                }
                debugLog('[loadTestSuites] 已加载测试套件:', response.count, '个', response.cached ? '(缓存)' : '(实时)');
                return testSuitesCache;
            } else {
                showToast('加载测试套件失败', 'error');
            }
        } catch (error) {
            console.error('[loadTestSuites] 错误:', error);
            showToast('加载测试套件失败: ' + error.message, 'error');
        } finally {
            _loadSuitesPromise = null;
        }
        return testSuitesCache;
    })();

    return _loadSuitesPromise;
}

async function refreshTestSuites() {
    const button = document.getElementById('refresh-suites-btn');
    if (button?.disabled) return;
    if (button) {
        button.disabled = true;
        button.textContent = '刷新中…';
        button.setAttribute('aria-busy', 'true');
    }
    showToast('正在刷新测试套件...', 'info');
    try {
        await loadTestSuites(true);
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = '↻ 刷新套件';
            button.removeAttribute('aria-busy');
        }
    }
}

function renderTestSuitesDropdown() {
    const selectElement = document.getElementById('test-suite');

    // 清空现有选项
    selectElement.innerHTML = '';

    // 添加空选项作为默认值
    const emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = '';
    emptyOption.disabled = true;
    emptyOption.selected = true;
    selectElement.appendChild(emptyOption);

    // 按测试类型分组
    const groupedSuites = {};
    testSuitesCache.forEach(suite => {
        // GTS-ROOT 使用 GTS 分组
        const groupType = suite.test_type.toLowerCase() === 'gts-root' ? 'gts' : suite.test_type.toLowerCase();
        if (!groupedSuites[groupType]) {
            groupedSuites[groupType] = [];
        }
        groupedSuites[groupType].push(suite);
    });

    // 添加分组选项
    Object.keys(groupedSuites).sort().forEach(testType => {
        const group = document.createElement('optgroup');
        group.label = testType.toUpperCase();

        groupedSuites[testType].forEach(suite => {
            const option = document.createElement('option');
            option.value = suite.tools_path;
            option.textContent = suite.tools_path;
            group.appendChild(option);
        });

        selectElement.appendChild(group);
    });

    // 渲染完成后，自动根据当前选择的测试类型来选择合适的测试套件
    const currentTestType = $('test-type')?.value;
    if (currentTestType) {
        autoSelectTestSuite(currentTestType);
    }
}


