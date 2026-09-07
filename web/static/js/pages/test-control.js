// ==================== Test Control ====================
async function toggleTest() {
    if (state.testing) {
        await stopTest();
    } else {
        await startTest();
    }
}

async function startTest() {
    if (state.testing) {
        showToast('测试已在运行中', 'warning');
        return;
    }

    if (!validateDeviceSelection()) return;

    // 前端最后一道 invariant：所有选中设备必须属于当前测试 Worker。
    // 通过 state.devices inventory 解析归属，绝不按 ":" 猜 Worker 前缀——
    // Android serial 可能含 ":"（ADB TCP "ip:5555"、ADB Proxy
    // "localhost:port"），字符串切分会误杀这类本机设备。
    const currentWorker = workspaceWorkerId();
    const resolveDeviceOwner = deviceId => {
        const device = state.devices.find(item => {
            const candidate = typeof item === 'string' ? item : item;
            return candidate === deviceId
                || candidate.device_id === deviceId
                || candidate.id === deviceId
                || candidate.serial === deviceId
                || candidate.serial_no === deviceId;
        });
        if (!device || typeof device === 'string') return null;
        return device.worker_id || device.cluster_worker_id || null;
    };
    const foreignDevices = Array.from(state.selectedDevices).filter(deviceId => {
        const owner = resolveDeviceOwner(deviceId);
        // inventory 里找不到（列表未加载/设备刚消失）时交给后端权威校验，
        // 前端只拦截"明确解析到其他 Worker"的情况。
        if (owner === null) return false;
        return owner !== currentWorker && !isLocalWorkspaceWorker(owner);
    });
    if (foreignDevices.length > 0) {
        showToast(
            `所选设备不属于当前测试 Worker ${currentWorker}，请重新选择: ${foreignDevices.join(', ')}`,
            'warning');
        return;
    }

    const testType = document.getElementById('test-type').value;
    const testModule = document.getElementById('test-module').value.trim();
    const testCase = document.getElementById('test-case').value.trim();
    const retryResult = document.getElementById('retry-result').value.trim();
    const suitePath = document.getElementById('test-suite')?.value?.trim() || '';

    if (!suitePath) {
        showToast('请先选择测试套件', 'warning');
        return;
    }

    try {
        if ('Notification' in window && Notification.permission === 'default' && !state.browserNotificationsEnabled) {
            void requestBrowserNotificationPermission();
        }

        // 只清当前 Worker scope 的日志，保留其他 Worker 的隐藏历史
        // 和无 scope 的全局条目。
        clearWorkerLogs(workspaceWorkerId());

        const startResult = await apiCall('/api/test/start', 'POST', {
            worker_id: workspaceWorkerId(),
            devices: Array.from(state.selectedDevices),
            test_type: testType,
            test_module: testModule,
            test_case: testCase,
            retry_dir: retryResult,
            test_suite: suitePath,
            local_server: state.config?.local_server || state.clientDisplayId || state.clientId || ''
        });
        const clusterJobId = startResult?.data?.cluster_job_id || startResult?.cluster_job_id || '';
        if (clusterJobId) {
            state.clusterJobId = clusterJobId;
            resetClusterEventCursor();
            sessionStorage.setItem('active_cluster_job', clusterJobId);
            window.GmsWorkspace?.update({
                worker_id: workspaceWorkerId(),
                device_ids: Array.from(state.selectedDevices),
                suite_key: suitePath,
                suite_path: suitePath,
                cluster_job_id: clusterJobId,
                attempt_id: startResult?.data?.attempt_id || startResult?.attempt_id || '',
                origin_page: 'test'
            }, {source: 'test-start'});
            addWorkerLog(workspaceWorkerId(), `分布式任务 ${clusterJobId} 已排队`, 'info');
        }

        debugLog('[startTest] API call successful, setting testing = true');
        state.testStopping = false;
        state.testing = true;
        updateTestToggleButton(true);
        addWorkerLog(workspaceWorkerId(), '测试已启动', 'success');
        showToast('测试已启动', 'success');
        switchLogTab('module');
        wakeTestStatusPolling();

        // 刷新设备列表以更新锁定状态
        await refreshDevices();
    } catch (error) {
        addLogEntry('启动测试失败: ' + error.message, 'error');
    }
}

async function stopTest() {
    if (!state.testing) {
        showToast('没有正在运行的测试', 'warning');
        return;
    }

    try {
        addLogEntry('⏹ 用户请求停止测试...', 'info');

        if (state.clusterJobId) {
            const workerId = workspaceWorkerId();
            await apiCall(`/api/cluster/jobs/${encodeURIComponent(state.clusterJobId)}/cancel`, 'POST');
            state.testStopping = true;
            updateTestToggleButton(true);
            addWorkerLog(workerId, '停止请求已发送，正在等待 Worker 结束任务...', 'warning');
            showToast('停止请求已发送', 'warning');
            wakeTestStatusPolling();
            return;
        } else {
            // 使用新的 stop 接口（支持多用户隔离）
            await apiCall('/api/test/stop', 'POST');
        }

        // Update test state
        state.testing = false;
        state.testStopping = false;
        state.clusterJobId = '';
        resetClusterEventCursor();
        sessionStorage.removeItem('active_cluster_job');
        window.GmsWorkspace?.update({cluster_job_id: '', attempt_id: ''}, {source: 'test-stop'});
        updateTestToggleButton(false);

        addLogEntry('测试已停止', 'warning');
        showToast('测试已停止', 'warning');

        // Refresh devices (强制刷新以获取最新状态)
        await loadDevices(true);
    } catch (error) {
        state.testStopping = false;
        updateTestToggleButton(state.testing);
        addLogEntry('停止测试失败: ' + error.message, 'error');
    }
}

function updateTestToggleButton(isTesting) {
    const btn = $('test-toggle-btn');
    if (!btn) return;

    btn.disabled = Boolean(state.testStopping);
    if (state.testStopping) {
        btn.textContent = '⏳ 停止中';
        btn.className = 'btn-danger btn-lg';
    } else if (isTesting) {
        btn.textContent = '⏹ 停止测试';
        btn.className = 'btn-danger btn-lg';
    } else {
        btn.textContent = '▶ 开始测试';
        btn.className = 'btn-primary btn-lg';
    }

    // 禁用/启用测试相关输入框
    const testInputs = [
        'test-type',      // 测试类型
        'test-module',    // 测试模块
        'test-case',      // 测试用例
        'test-suite',     // 测试套件
        'retry-result'    // 测试报告
    ];

    testInputs.forEach(id => {
        const element = document.getElementById(id);
        if (element) {
            element.disabled = isTesting;
        }
    });

    // 测试主机下拉框在测试期间也保持可用：切换主机不会中断正在运行的测试
    // （测试在后端按 clusterJobId 运行，停止操作也通过 clusterJobId 执行）。
    const workerSelect = document.getElementById('cluster-worker');
    if (workerSelect) {
        const clusterEnabled = Boolean(
            state.clusterStatus?.enabled
            && window.GmsWorkspace?.get?.().scope_mode === 'cluster'
        );
        const workersLoaded = workerSelect.dataset.workersLoaded === 'true';
        workerSelect.disabled = !clusterEnabled || !workersLoaded;
        workerSelect.title = clusterEnabled
            ? (workersLoaded
                ? '选择执行测试的 Cluster Worker'
                : '正在加载测试主机列表')
            : '当前为单机模式；切换到集群模式后可选择远端测试主机';
    }

    // 禁用/启用浏览按钮
    const browseButtons = document.querySelectorAll('button[onclick*="browseRemoteFile"]');
    browseButtons.forEach(btn => {
        if (btn.getAttribute('onclick').includes('suite') || btn.getAttribute('onclick').includes('retry')) {
            btn.disabled = isTesting;
        }
    });
}

async function cleanTest() {
    try {
        const workerId = workspaceWorkerId();
        if (isLocalWorkspaceWorker(workerId)) {
            await apiCall('/api/test/clean', 'POST');
        }
        // 只清当前 Worker scope 的日志，保留其他 Worker 的隐藏历史。
        clearWorkerLogs(workerId);
        addWorkerLog(workerId, '测试日志已清除', 'info');
    } catch (error) {
        addLogEntry('清除日志失败: ' + error.message, 'error');
    }
}

async function downloadTestLog() {
    try {
        addLogEntry('正在保存日志...', 'info');

        // 拼接两个日志容器的实际内容（系统日志 + 测试日志）
        const systemOut = getLogContainer('system');
        const moduleOut = getLogContainer('module');
        const systemContent = systemOut ? systemOut.innerText : '';
        const moduleContent = moduleOut ? moduleOut.innerText : '';
        const logContent = [
            systemContent.trim() ? `===== 系统日志 =====\n${systemContent.trim()}` : '',
            moduleContent.trim() ? `===== 测试日志 =====\n${moduleContent.trim()}` : '',
        ].filter(Boolean).join('\n\n');

        if (!logContent.trim()) {
            showToast('没有可保存的日志内容', 'warning');
            return;
        }

        // 发送日志内容到后端保存（test_type 直接读下拉框：state.testType 无人维护）
        const saveResult = await apiCall('/api/test/logs/save', 'POST', {
            content: logContent,
            test_type: document.getElementById('test-type')?.value || state.testType || 'unknown'
        });

        if (saveResult.success) {
            addLogEntry(`✅ 日志已保存: ${saveResult.filename}`, 'success');
            triggerDownload('/api/test/logs/get', saveResult.filename);
            showToast(`日志已保存并下载: ${saveResult.filename}`, 'success');
        } else {
            throw new Error(saveResult.error || '保存失败');
        }
    } catch (error) {
        addLogEntry('保存日志失败: ' + error.message, 'error');
        showToast('保存日志失败: ' + error.message, 'error');
    }
}

async function showConfig() {
    const modal = document.getElementById('config-modal');
    const modalBody = document.getElementById('config-modal-body');

    // Fetch current config from API
    let config = {};
    try {
        config = await apiCall('/api/config/read', 'GET');
    } catch (error) {
        addLogEntry('获取配置失败: ' + error.message, 'error');
        return;
    }
    const usbipVidPids = Array.isArray(config.usbip_vid_pids)
        ? config.usbip_vid_pids.join(', ')
        : String(config.usbip_vid_pid || '');

    // Generate config form with actual values
    modalBody.innerHTML = `
        <form onsubmit="event.preventDefault(); saveConfig();" autocomplete="off">
        <div class="modal-form-row">
            <label>测试主机用户:</label>
            <input type="text" id="config-ubuntu-user" value="${config.ubuntu_user || ''}" autocomplete="username" />
        </div>
        <div class="modal-form-row">
            <label>测试主机地址:</label>
            <input type="text" id="config-ubuntu-host" value="${config.ubuntu_host || ''}" />
        </div>
        <div class="modal-form-row">
            <label>测试主机密码:</label>
            <input type="password" id="config-ubuntu-pswd" placeholder="输入测试主机SSH密码(留空保持不变)" autocomplete="current-password" />
        </div>
        <div class="modal-form-row">
            <label>设备主机地址:</label>
            <input type="text" id="config-device-host" value="${config.device_host || ''}" />
        </div>
        <div class="modal-form-row">
            <label>设备主机密码:</label>
            <input type="password" id="config-device-pswd" placeholder="输入设备主机SSH密码(留空保持不变)" autocomplete="current-password" />
        </div>
        <div class="modal-form-row">
            <label>本地主机地址:</label>
            <input type="text" id="config-local-server" value="${config.local_server || ''}" />
        </div>
        <div class="modal-form-row">
            <label>设备VID:PID:</label>
            <input type="text" id="config-usbip-vid-pids" value="${escapeHtml(usbipVidPids)}" placeholder="例如: 2207:0006, 18d1:4d00" />
        </div>
        <div class="modal-form-row">
            <label>测试脚本路径:</label>
            <input type="text" id="config-script-path" class="readonly" value="${config.script_path || ''}" readonly />
        </div>
        <div class="modal-form-row">
            <label>测试套件路径:</label>
            <input type="text" id="config-suites-path" value="${config.suites_path || ''}" />
        </div>

        <div style="margin-top:14px;border-top:1px solid var(--border-color);padding-top:10px;">
            <div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:8px;">日志显示设置</div>
            <div class="modal-form-row">
                <label>历史日志条数:</label>
                <input type="number" id="config-log-history-limit" min="10" max="2000"
                       value="${localStorage.getItem('gms-log-history-limit') || 100}" />
            </div>
            <div class="modal-form-row">
                <label>实时日志上限:</label>
                <input type="number" id="config-log-max-entries" min="50" max="10000"
                       value="${localStorage.getItem('gms-log-max-entries') || 1000}" />
            </div>
            <small style="color:var(--text-secondary);">历史日志：首次加载状态时显示的条数；实时日志：页面每个Tab最多保留的条数。保存后即时生效。</small>
        </div>
        </form>

        <!-- 客户端 SSH 凭据管理（增删独立于上方静态配置，即时生效） -->
        <div class="modal-form-row" style="flex-direction:column;align-items:stretch;margin-top:14px;">
            <label style="margin-bottom:6px;">客户端 SSH 凭据:</label>
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <thead>
                    <tr style="background: var(--darker-bg);">
                        <th style="padding:5px 6px;text-align:left;">设备主机</th>
                        <th style="padding:5px 6px;text-align:left;">用户名</th>
                        <th style="padding:5px 6px;text-align:left;">密码</th>
                        <th style="padding:5px 6px;text-align:center;">操作</th>
                    </tr>
                </thead>
                <tbody id="client-creds-table-body">
                    <tr><td colspan="4" style="padding:20px;text-align:center;">加载中...</td></tr>
                </tbody>
            </table>
            <div class="modal-form-row" style="margin-top:8px;gap:6px;">
                <input type="text" id="client-cred-host" placeholder="user@ip，例如 gms@192.168.1.100" autocomplete="off" style="flex:2;" />
                <input type="password" id="client-cred-password" placeholder="SSH 密码" autocomplete="new-password" style="flex:1.5;" />
                <button class="btn-xxs btn-primary" onclick="addClientCredential()">添加/更新</button>
            </div>
            <small style="color:var(--text-secondary);margin-top:4px;">填入已有主机的地址+新密码可覆盖更新；密码不会回显明文。</small>
        </div>

        <!-- 静态路由管理（增删独立于上方静态配置，保存后立即应用到本机路由表） -->
        <div class="modal-form-row" style="flex-direction:column;align-items:stretch;margin-top:14px;">
            <label style="margin-bottom:6px;">静态路由（启动时与保存后自动应用）:</label>
            <div class="modal-form-row" style="gap:8px;margin-bottom:6px;">
                <label style="margin:0;">
                    <input type="checkbox" id="config-static-routes-enabled" style="vertical-align:middle;" />
                    启用静态路由
                </label>
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <thead>
                    <tr style="background: var(--darker-bg);">
                        <th style="padding:5px 6px;text-align:left;">目标网段</th>
                        <th style="padding:5px 6px;text-align:left;">网关</th>
                        <th style="padding:5px 6px;text-align:center;">操作</th>
                    </tr>
                </thead>
                <tbody id="static-routes-table-body">
                    <tr><td colspan="3" style="padding:20px;text-align:center;">加载中...</td></tr>
                </tbody>
            </table>
            <div class="modal-form-row" style="margin-top:8px;gap:6px;">
                <input type="text" id="static-route-destination" placeholder="目标网段，例如 10.10.10.0/24 或 10.10.10.29/32" autocomplete="off" style="flex:2;" />
                <input type="text" id="static-route-gateway" placeholder="网关，例如 172.16.14.1" autocomplete="off" style="flex:1.5;" />
                <button class="btn-xxs btn-primary" onclick="addStaticRouteRow()">添加</button>
            </div>
            <small style="color:var(--text-secondary);margin-top:4px;">程序启动和每次保存时自动应用（幂等）。修改路由表需要 root；普通用户运行时需配置 sudoers 免密 ip route。</small>
        </div>
    `;

    ModalManager.open('config-modal');
    const footer = document.getElementById('config-modal-footer');
    if (footer) footer.style.display = '';
    loadClientCredentials();
    loadStaticRoutes();
}

async function showGmsAssistantConfig() {
    const input = document.getElementById('gms-assistant-url');
    if (!input) return;
    try {
        const result = await apiCall('/api/config/external-services', 'GET');
        const data = result?.data || result || {};
        input.value = String(data.gms_assistant_url || '').trim();
        ModalManager.open('gms-assistant-config-modal');
    } catch (error) {
        showToast('读取 GMS助手配置失败: ' + error.message, 'error');
    }
}

async function saveGmsAssistantConfig() {
    const input = document.getElementById('gms-assistant-url');
    const url = String(input?.value || '').trim();
    if (url && !/^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(url)) {
        showToast('请输入完整的 http(s) 地址', 'warning');
        return;
    }
    try {
        await apiCall('/api/config/external-services', 'POST', {gms_assistant_url: url});
        ModalManager.close('gms-assistant-config-modal');
        showToast('GMS助手配置已保存', 'success');
        const frame = document.getElementById('gms-assistant-frame');
        const dataSrc = frame?.getAttribute('data-src');
        if (frame && dataSrc) {
            window.setLazyFrameSource?.(frame, dataSrc);
        }
    } catch (error) {
        showToast('保存 GMS助手配置失败: ' + error.message, 'error');
    }
}

// 客户端 SSH 凭据管理。
async function loadClientCredentials() {
    const tbody = document.getElementById('client-creds-table-body');
    if (!tbody) return;
    let credentials = [];
    try {
        const result = await apiCall('/api/config/client-ssh-credentials', 'GET');
        credentials = (result && (result.credentials || result.data?.credentials)) || [];
    } catch (error) {
        renderClientCredentialsMessage(tbody, `加载失败: ${error.message}`, 'var(--error-color)');
        return;
    }

    if (!credentials.length) {
        renderClientCredentialsMessage(tbody, '暂无凭据。在下方添加设备主机 SSH 凭据。', 'var(--text-secondary)');
        return;
    }

    tbody.replaceChildren();
    credentials.forEach(cred => {
        const host = cred.device_host || `${cred.username || ''}@${cred.host || ''}`.replace(/^@$/, '');
        const masked = cred.has_password ? '••••••' : '未设置';
        const row = document.createElement('tr');
        row.style.borderBottom = '1px solid var(--border-color)';

        appendClientCredentialCell(row, host || '-');
        appendClientCredentialCell(row, cred.username || '-');
        appendClientCredentialCell(row, masked, 'var(--text-secondary)');

        const actionCell = appendClientCredentialCell(row, '');
        actionCell.style.textAlign = 'center';
        const deleteButton = document.createElement('button');
        deleteButton.className = 'btn-xxs';
        deleteButton.type = 'button';
        deleteButton.textContent = '删除';
        deleteButton.addEventListener('click', () => deleteClientCredential(host));
        actionCell.appendChild(deleteButton);

        tbody.appendChild(row);
    });
}

function renderClientCredentialsMessage(tbody, message, color) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.style.padding = '20px';
    cell.style.textAlign = 'center';
    if (color) cell.style.color = color;
    cell.textContent = message;
    row.appendChild(cell);
    tbody.replaceChildren(row);
}

function appendClientCredentialCell(row, text, color) {
    const cell = document.createElement('td');
    cell.style.padding = '4px 6px';
    if (color) cell.style.color = color;
    cell.textContent = text;
    row.appendChild(cell);
    return cell;
}

async function addClientCredential() {
    const hostInput = document.getElementById('client-cred-host');
    const pwdInput = document.getElementById('client-cred-password');
    const deviceHost = (hostInput?.value || '').trim();
    const password = pwdInput?.value || '';

    if (!/^[^@\s<>"'`]+@[^@\s<>"'`]+$/.test(deviceHost)) {
        showToast('请输入 user@ip 格式的设备主机', 'warning');
        return;
    }
    if (!password) {
        showToast('请输入密码', 'warning');
        return;
    }

    try {
        await apiCall('/api/config/client-ssh-credentials', 'POST', { device_host: deviceHost, password });
        showToast('凭据已保存', 'success');
        if (hostInput) hostInput.value = '';
        if (pwdInput) pwdInput.value = '';
        await loadClientCredentials();
    } catch (error) {
        showToast('保存凭据失败: ' + error.message, 'error');
    }
}

async function deleteClientCredential(deviceHost) {
    if (!deviceHost) return;
    if (!await showConfirmDialog(
        '删除 SSH 凭据',
        `确认删除凭据：${deviceHost}？`
    )) return;
    try {
        await apiCall('/api/config/client-ssh-credentials', 'DELETE', { device_host: deviceHost });
        showToast('凭据已删除', 'success');
        await loadClientCredentials();
    } catch (error) {
        showToast('删除凭据失败: ' + error.message, 'error');
    }
}

// ==================== 静态路由管理 ====================
// 配置保存在运行时配置（configs/config_runtime.json），保存后立即应用到本机路由表。

function _readStaticRouteRows() {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return [];
    return Array.from(tbody.querySelectorAll('tr[data-destination]')).map(row => ({
        destination: row.dataset.destination || '',
        gateway: row.dataset.gateway || '',
    }));
}

async function loadStaticRoutes() {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return;
    const enabledCheckbox = document.getElementById('config-static-routes-enabled');
    let data = {};
    try {
        const result = await apiCall('/api/config/static-routes', 'GET');
        data = (result && (result.data || result)) || {};
    } catch (error) {
        renderStaticRoutesMessage(`加载失败: ${error.message}`, 'var(--error-color)');
        return;
    }
    if (enabledCheckbox) enabledCheckbox.checked = !!data.enabled;
    renderStaticRoutesTable(Array.isArray(data.routes) ? data.routes : []);
}

function renderStaticRoutesMessage(message, color) {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="3" style="padding:20px;text-align:center;${color ? `color:${color};` : ''}">${escapeHtml(message)}</td></tr>`;
}

function renderStaticRoutesTable(routes) {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return;
    if (!routes.length) {
        renderStaticRoutesMessage('暂无静态路由。在下方添加目标网段和网关。', 'var(--text-secondary)');
        return;
    }
    tbody.innerHTML = routes.map(route => `
        <tr data-destination="${escapeHtml(route.destination)}" data-gateway="${escapeHtml(route.gateway)}" style="border-bottom: 1px solid var(--border-color);">
            <td style="padding:5px 6px;font-family:monospace;">${escapeHtml(route.destination)}</td>
            <td style="padding:5px 6px;font-family:monospace;">${escapeHtml(route.gateway)}</td>
            <td style="padding:5px 6px;text-align:center;">
                <button class="btn-xxs" onclick="deleteStaticRouteRow(this)">删除</button>
            </td>
        </tr>
    `).join('');
}

function addStaticRouteRow() {
    const destInput = document.getElementById('static-route-destination');
    const gwInput = document.getElementById('static-route-gateway');
    const destination = (destInput?.value || '').trim();
    const gateway = (gwInput?.value || '').trim();

    if (!/^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/.test(destination)) {
        showToast('目标网段格式应为 a.b.c.d/掩码，例如 10.10.10.0/24', 'warning');
        return;
    }
    if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(gateway)) {
        showToast('网关格式应为 IP 地址，例如 172.16.14.1', 'warning');
        return;
    }

    const routes = _readStaticRouteRows();
    if (routes.some(r => r.destination === destination && r.gateway === gateway)) {
        showToast('该路由已存在', 'warning');
        return;
    }
    routes.push({destination, gateway});
    renderStaticRoutesTable(routes);
    if (destInput) destInput.value = '';
    if (gwInput) gwInput.value = '';
}

function deleteStaticRouteRow(button) {
    const row = button?.closest('tr[data-destination]');
    if (row) row.remove();
}

async function saveStaticRoutes() {
    const enabledCheckbox = document.getElementById('config-static-routes-enabled');
    const payload = {
        enabled: !!(enabledCheckbox && enabledCheckbox.checked),
        routes: _readStaticRouteRows(),
    };
    try {
        const result = await apiCall('/api/config/static-routes', 'POST', payload);
        const data = (result && (result.data || result)) || {};
        if (data.success) {
            showToast('静态路由已保存并应用', 'success');
        } else {
            showToast(data.error || '路由已保存，但应用失败（需要 root/sudoers 免密权限）', 'warning');
        }
        renderStaticRoutesTable(payload.routes);
    } catch (error) {
        showToast('保存静态路由失败: ' + error.message, 'error');
    }
}

function closeModal(modalId) {
    const id = modalId || 'config-modal';
    const modal = document.getElementById(id);
    if (modal) {
        // 对于动态创建的模态框（直接移除）
        if (id.startsWith('source-analysis-modal-') || id.startsWith('ai-analysis-modal-')) {
            // 先从 ModalManager 移除（清理 Esc 监听器）
            ModalManager.close(id);

            modal.style.display = 'none';
            // 延迟删除，确保动画完成
            setTimeout(() => {
                if (modal && modal.parentNode) {
                    modal.parentNode.removeChild(modal);
                }
            }, 300);
        } else {
            // 对于静态模态框（使用class控制）
            ModalManager.close(id);
        }
    }
}

async function saveConfig() {
    const ubuntuPassword = document.getElementById('config-ubuntu-pswd').value;
    const devicePassword = document.getElementById('config-device-pswd').value;
    const usbipVidPids = document.getElementById('config-usbip-vid-pids').value
        .split(/[,;\s]+/)
        .map(value => value.trim().toLowerCase())
        .filter(Boolean);
    const config = {
        ubuntu_user: document.getElementById('config-ubuntu-user').value,
        ubuntu_host: document.getElementById('config-ubuntu-host').value,
        device_host: document.getElementById('config-device-host').value,
        local_server: document.getElementById('config-local-server').value,
        suites_path: document.getElementById('config-suites-path').value,
        usbip_vid_pids: [...new Set(usbipVidPids)]
    };

    // Only include passwords if they are not empty
    if (ubuntuPassword) {
        config.ubuntu_pswd = ubuntuPassword;
    }
    if (devicePassword) {
        config.device_pswd = devicePassword;
    }

    // Save UI log settings to localStorage (frontend-only)
    const historyLimit = parseInt(document.getElementById('config-log-history-limit')?.value) || 100;
    const maxEntries = parseInt(document.getElementById('config-log-max-entries')?.value) || 1000;
    localStorage.setItem('gms-log-history-limit', String(historyLimit));
    localStorage.setItem('gms-log-max-entries', String(maxEntries));

    try {
        addLogEntry('正在保存配置...', 'info');
        showToast('正在保存配置...', 'info');

        // 立即关闭模态框
        closeModal();

        await apiCall('/api/config/update', 'POST', config);
        addLogEntry('配置已保存', 'success');
        showToast('配置保存成功', 'success');

        // 静态路由独立保存（保存即应用到本机路由表），失败不阻塞主配置。
        try {
            await saveStaticRoutes();
        } catch (routeError) {
            addLogEntry('静态路由保存失败: ' + routeError.message, 'error');
        }

        // Reload page to update config values
        setTimeout(() => location.reload(), 500);
    } catch (error) {
        addLogEntry('保存配置失败: ' + error.message, 'error');
        showToast('保存失败: ' + error.message, 'error');
    }
}
