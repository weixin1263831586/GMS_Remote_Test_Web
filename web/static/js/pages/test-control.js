// ==================== Test Control ====================
// Start/Stop 按钮是四态状态机：IDLE → STARTING → RUNNING → STOPPING → IDLE。
// 过渡态（testStarting/testStopping）必须在 await 之前置位：JS 事件循环下
// “先 await 再置位”会留一个窗口，快速双击触发两次 click 时第二个 click
// 看到的仍是旧状态，于是重复 POST /api/test/start（或重复 stop）。

async function toggleTest() {
    if (state.testStarting || state.testStopping) return;
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
    if (state.testStarting) {
        showToast('测试正在启动，请稍候', 'info');
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

    // 进入 STARTING：先置过渡态再发请求，双击在这里被挡住。
    state.testStarting = true;
    updateTestToggleButton(false);
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

        // ---- 核心操作成功：立即 commit RUNNING，后续任何失败都不回滚 ----
        state.testStarting = false;
        state.testStopping = false;
        state.testing = true;

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

        updateTestToggleButton(true);
        addWorkerLog(workspaceWorkerId(), '测试已启动', 'success');
        showToast('测试已启动', 'success');
        switchLogTab('module');
        wakeTestStatusPolling();

        // ---- 后置刷新：属于锦上添花，失败只警告。不能与上面的核心操作
        // 共用 try/catch——refreshDevices 因瞬时 SSH/API 抖动抛错时，用户
        // 会同时看到“测试已启动”和“启动测试失败”，然后再次点启动。
        try {
            await refreshDevices();
        } catch (refreshError) {
            addLogEntry('启动成功，但刷新设备列表失败: ' + refreshError.message, 'warning');
        }
    } catch (error) {
        // 只有核心 POST /api/test/start 失败才走到这里。
        state.testStarting = false;
        updateTestToggleButton(Boolean(state.testing));
        addLogEntry('启动测试失败: ' + error.message, 'error');
    }
}

async function stopTest() {
    if (state.testStopping) {
        showToast('停止请求已发送，请等待任务结束', 'info');
        return;
    }
    if (!state.testing) {
        showToast('没有正在运行的测试', 'warning');
        return;
    }

    // 进入 STOPPING：同样先置过渡态再发请求。
    state.testStopping = true;
    updateTestToggleButton(true);
    try {
        addLogEntry('⏹ 用户请求停止测试...', 'info');

        if (state.clusterJobId) {
            const workerId = workspaceWorkerId();
            await apiCall(`/api/cluster/jobs/${encodeURIComponent(state.clusterJobId)}/cancel`, 'POST');
            addWorkerLog(workerId, '停止请求已发送，正在等待 Worker 结束任务...', 'warning');
            showToast('停止请求已发送', 'warning');
            wakeTestStatusPolling();
            return; // 保持 STOPPING，由状态轮询在 job 结束后回 IDLE
        }

        // 使用新的 stop 接口（支持多用户隔离）
        await apiCall('/api/test/stop', 'POST');

        // ---- 核心操作成功：立即 commit IDLE ----
        state.testing = false;
        state.testStopping = false;
        state.clusterJobId = '';
        resetClusterEventCursor();
        sessionStorage.removeItem('active_cluster_job');
        window.GmsWorkspace?.update({cluster_job_id: '', attempt_id: ''}, {source: 'test-stop'});
        updateTestToggleButton(false);

        addLogEntry('测试已停止', 'warning');
        showToast('测试已停止', 'warning');

        // ---- 后置刷新：失败只警告，不说“停止失败” ----
        try {
            await loadDevices(true);
        } catch (refreshError) {
            addLogEntry('停止成功，但刷新设备列表失败: ' + refreshError.message, 'warning');
        }
    } catch (error) {
        // 只有 cancel/stop 请求本身失败才走到这里。
        state.testStopping = false;
        updateTestToggleButton(state.testing);
        addLogEntry('停止测试失败: ' + error.message, 'error');
    }
}

function updateTestToggleButton(isTesting) {
    // 调用方传入的 boolean 只是 testing(RUNNING) 的同步；最终渲染完全由
    // 状态机四态推导，避免轮询回调把过渡态（启动中/停止中）冲掉。
    if (typeof isTesting === 'boolean') {
        state.testing = isTesting;
    }
    const btn = $('test-toggle-btn');
    if (!btn) return;

    if (state.testStopping) {
        btn.disabled = true;
        btn.textContent = '⏳ 停止中';
        btn.className = 'btn-danger btn-lg';
    } else if (state.testStarting) {
        btn.disabled = true;
        btn.textContent = '⏳ 启动中';
        btn.className = 'btn-primary btn-lg';
    } else if (state.testing) {
        btn.disabled = false;
        btn.textContent = '⏹ 停止测试';
        btn.className = 'btn-danger btn-lg';
    } else {
        btn.disabled = false;
        btn.textContent = '▶ 开始测试';
        btn.className = 'btn-primary btn-lg';
    }

    // 禁用/启用测试配置控件（CSP 迁移后统一契约：data-test-config-control，
    // 覆盖测试类型/模块/用例/套件/报告输入及“📁 选择报告”浏览按钮；
    // 不再按 handler 名（onclick*=）猜测，避免 data-click 迁移后失配）。
    // 三个非 IDLE 态（启动中/运行中/停止中）都保持禁用。
    const controlsDisabled = Boolean(state.testStarting || state.testing || state.testStopping);
    document.querySelectorAll('[data-test-config-control]').forEach(element => {
        element.disabled = controlsDisabled;
    });

    // 保留 id 兜底：历史 DOM（如测试快照）可能尚无统一标记。
    ['test-type', 'test-module', 'test-case', 'test-suite', 'retry-result']
        .forEach(id => {
            const element = document.getElementById(id);
            if (element) element.disabled = controlsDisabled;
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

    // 浏览按钮随统一契约一并禁用（上面 data-test-config-control 已覆盖）。
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
    // ADR：结构用 HTML、数据用 DOM property —— 模板不插入任何配置数据，
    // 避免 " < > & 破坏属性 / DOM 注入 / 字段消失类 UI 完整性问题。
    modalBody.innerHTML = `
        <div class="config-section config-section-notice">
            部署拓扑（用户/主机/路径/VID:PID）为静态配置：请由管理员在
            <code>configs/local/config.json</code> 修改后重启生效；此处只读展示，
            密码经环境变量（GMS_UBUNTU_PASSWORD 等）注入，不在 Web 端保存。
        </div>
        <form id="test-control-config-form" autocomplete="off">
        <div class="config-section config-section-accent-deploy">
            <div class="config-section-title">部署拓扑（只读）</div>
            <div class="modal-form-row">
                <label>测试主机用户:</label>
                <input type="text" id="config-ubuntu-user" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>测试主机地址:</label>
                <input type="text" id="config-ubuntu-host" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>设备主机地址:</label>
                <input type="text" id="config-device-host" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>本地主机地址:</label>
                <input type="text" id="config-local-server" />
            </div>
            <div class="modal-form-row">
                <label>设备VID:PID:</label>
                <input type="text" id="config-usbip-vid-pids" class="readonly" readonly placeholder="例如: 2207:0006, 18d1:4d00" />
            </div>
            <div class="modal-form-row">
                <label>测试脚本路径:</label>
                <input type="text" id="config-script-path" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>测试套件路径:</label>
                <input type="text" id="config-suites-path" class="readonly" readonly />
            </div>
        </div>

        <div class="config-section config-section-accent-logs">
            <div class="config-section-title">日志显示设置</div>
            <div class="modal-form-row">
                <label>历史日志条数:</label>
                <input type="number" id="config-log-history-limit" min="10" max="2000" />
            </div>
            <div class="modal-form-row">
                <label>实时日志上限:</label>
                <input type="number" id="config-log-max-entries" min="50" max="10000" />
            </div>
            <small>历史日志：首次加载状态时显示的条数；实时日志：页面每个Tab最多保留的条数。保存后即时生效。</small>
        </div>
        </form>

        <!-- 邮件发送方（系统级）：全局 SMTP，保存走 /api/email/smtp（admin） -->
        <div class="config-section config-section-accent-email">
            <div class="config-section-title">邮件发送方（系统级 SMTP）</div>
            <div class="modal-form-row">
                <label>启用邮件发送:</label>
                <label style="margin:0;">
                    <input type="checkbox" id="config-email-enabled" style="vertical-align:middle;" />
                    开启后 Redmine 提醒、ATS 通知、运维告警等全部邮件出口生效
                </label>
            </div>
            <div class="modal-form-row">
                <label>SMTP 服务器:</label>
                <input type="text" id="config-email-smtp-host" placeholder="例如: smtphz.qiye.163.com" />
            </div>
            <div class="modal-form-row">
                <label>端口:</label>
                <input type="number" id="config-email-smtp-port" min="1" max="65535" placeholder="465" />
            </div>
            <div class="modal-form-row">
                <label>SMTP 用户名:</label>
                <input type="text" id="config-email-username" autocomplete="off" />
            </div>
            <div class="modal-form-row">
                <label>发件人地址:</label>
                <input type="text" id="config-email-from-addr" placeholder="留空则使用用户名" autocomplete="off" />
            </div>
            <div class="modal-form-row">
                <label>授权码/密码:</label>
                <input type="password" id="config-email-password" placeholder="留空保持不变" autocomplete="new-password" />
            </div>
            <small id="config-email-status">接收方邮箱在各业务处配置（如 Redmine 用户资料、运维告警 email_to），不在此处。</small>
            <div style="margin-top:10px;display:flex;align-items:center;gap:8px;">
                <button class="btn-xxs" id="email-outbox-refresh-btn" type="button">↻ 刷新历史邮件</button>
                <label style="margin:0;font-size:12px;">通道:
                    <select id="email-outbox-channel" style="font-size:12px;">
                        <option value="">全部</option>
                        <option value="manual">手动发送</option>
                        <option value="automation">自动化通知</option>
                        <option value="ops">运维告警</option>
                        <option value="redmine_reminder">Redmine 提醒</option>
                    </select>
                </label>
                <label style="margin:0;font-size:12px;">结果:
                    <select id="email-outbox-sent" style="font-size:12px;">
                        <option value="">全部</option>
                        <option value="true">成功</option>
                        <option value="false">失败</option>
                    </select>
                </label>
            </div>
            <div id="email-outbox-list" style="margin-top:6px;max-height:220px;overflow:auto;border:1px solid var(--border-color,#333);border-radius:4px;">
                <div style="padding:14px;text-align:center;color:var(--text-muted,#888);">点击「刷新历史邮件」查看本人邮件记录</div>
            </div>
            <small>记录每次出站邮件（含失败尝试）：时间、通道、收件人、主题与结果；正文仅存预览。自动化/运维邮件回链到对应任务/事件。</small>
        </div>

        <!-- 客户端 SSH 凭据管理（增删独立于上方静态配置，即时生效） -->
        <div class="config-section config-section-accent-creds">
            <div class="config-section-title">客户端 SSH 凭据</div>
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
                <button class="btn-xxs btn-primary" id="client-cred-add-btn" type="button">添加/更新</button>
            </div>
            <small>填入已有主机的地址+新密码可覆盖更新；密码不会回显明文。</small>
        </div>

        <!-- 静态路由管理（增删独立于上方静态配置，保存后立即应用到本机路由表） -->
        <div class="config-section config-section-accent-routes">
            <div class="config-section-title">静态路由（启动时与保存后自动应用）</div>
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
                <input type="text" id="static-route-gateway" placeholder="网关，例如 192.0.2.1" autocomplete="off" style="flex:1.5;" />
                <button class="btn-xxs btn-primary" id="static-route-add-btn" type="button">添加</button>
            </div>
            <small>程序启动和每次保存时自动应用（幂等）。修改路由表需要 root；普通用户运行时需配置 sudoers 免密 ip route。</small>
        </div>
    `;

    // 数据通过 DOM property 写入（不进 HTML 字符串）。
    const configValues = {
        'config-ubuntu-user': config.ubuntu_user,
        'config-ubuntu-host': config.ubuntu_host,
        'config-device-host': config.device_host,
        'config-local-server': config.local_server,
        'config-usbip-vid-pids': usbipVidPids,
        'config-script-path': config.script_path,
        'config-suites-path': config.suites_path,
        'config-log-history-limit':
            localStorage.getItem('gms-log-history-limit') || 100,
        'config-log-max-entries':
            localStorage.getItem('gms-log-max-entries') || 1000,
    };
    Object.entries(configValues).forEach(([id, value]) => {
        const input = document.getElementById(id);
        if (input) input.value = value ?? '';
    });

    ModalManager.open('config-modal');
    const footer = document.getElementById('config-modal-footer');
    if (footer) footer.style.display = '';
    _bindConfigModalHandlers(modalBody);
    loadClientCredentials();
    loadStaticRoutes();
    loadEmailSenderConfig();
    loadEmailOutbox();
}

// 历史邮件（outbox ledger）：本人视角；管理员如需全量走 GET /api/email/outbox?admin_view=true。
const EMAIL_OUTBOX_CHANNEL_LABELS = {
    manual: '手动',
    automation: '自动化',
    ops: '运维告警',
    redmine_reminder: 'Redmine',
};

async function loadEmailOutbox() {
    const listEl = document.getElementById('email-outbox-list');
    if (!listEl) return;
    const channel = document.getElementById('email-outbox-channel')?.value || '';
    const sent = document.getElementById('email-outbox-sent')?.value || '';
    listEl.innerHTML = '<div style="padding:14px;text-align:center;color:var(--text-muted,#888);">加载中...</div>';
    try {
        const params = new URLSearchParams({limit: '30'});
        if (channel) params.set('channel', channel);
        if (sent) params.set('sent', sent);
        const result = await apiCall(`/api/email/outbox?${params.toString()}`, 'GET');
        const data = result?.data || {};
        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            listEl.innerHTML = '<div style="padding:14px;text-align:center;color:var(--text-muted,#888);">暂无发件记录（筛选条件下无匹配）</div>';
            return;
        }
        const rows = items.map((item) => {
            const state = item.sent
                ? '<span style="color:var(--success-color,#4caf50);">✓ 成功</span>'
                : `<span style="color:var(--error-color,#f44336);" title="${escapeJsAttr(item.error || '')}">✗ 失败</span>`;
            const channelLabel = EMAIL_OUTBOX_CHANNEL_LABELS[item.channel] || escapeJsAttr(item.channel || '未知');
            const ref = item.ref_type && item.ref_id
                ? `<span title="${escapeJsAttr(item.ref_type + ':' + item.ref_id)}" style="color:var(--text-muted,#888);"> ↳${escapeJsAttr(String(item.ref_id).slice(0, 14))}</span>`
                : '';
            const toText = (item.to_addrs || []).join(', ');
            const preview = String(item.subject || '') +
                (item.body_preview ? `　<span style="color:var(--text-muted,#888);">${escapeJsAttr(item.body_preview.slice(0, 60))}…</span>` : '');
            return `<tr>
                <td style="padding:4px 6px;white-space:nowrap;">${escapeJsAttr(item.created_at || '')}</td>
                <td style="padding:4px 6px;white-space:nowrap;">${channelLabel}${ref}</td>
                <td style="padding:4px 6px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeJsAttr(toText)}">${escapeJsAttr(toText)}</td>
                <td style="padding:4px 6px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeJsAttr(item.subject || '')}">${preview}</td>
                <td style="padding:4px 6px;white-space:nowrap;">${state}</td>
            </tr>`;
        }).join('');
        listEl.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px;">
            <thead><tr style="background:var(--darker-bg,#222);">
                <th style="padding:4px 6px;text-align:left;">时间(UTC)</th>
                <th style="padding:4px 6px;text-align:left;">通道</th>
                <th style="padding:4px 6px;text-align:left;">收件人</th>
                <th style="padding:4px 6px;text-align:left;">主题/预览</th>
                <th style="padding:4px 6px;text-align:left;">结果</th>
            </tr></thead><tbody>${rows}</tbody></table>
            <div style="padding:4px 6px;color:var(--text-muted,#888);font-size:11px;">共 ${data.total} 条，显示最近 ${items.length} 条</div>`;
    } catch (error) {
        listEl.innerHTML = `<div style="padding:14px;text-align:center;color:var(--error-color,#f44336);">加载历史邮件失败: ${escapeJsAttr(error.message)}</div>`;
    }
}

// 邮件发送方（系统级 SMTP）：读全局配置回填；密码永不回显，留空表示保持不变。
async function loadEmailSenderConfig() {
    const status = document.getElementById('config-email-status');
    try {
        const result = await apiCall('/api/email/smtp', 'GET');
        const data = result?.data || result || {};
        const setValue = (id, value) => {
            const input = document.getElementById(id);
            if (input) input.value = value ?? '';
        };
        setValue('config-email-smtp-host', data.smtp_host);
        setValue('config-email-smtp-port', data.smtp_port || 465);
        setValue('config-email-username', data.username);
        setValue('config-email-from-addr', data.from_addr);
        const enabledInput = document.getElementById('config-email-enabled');
        if (enabledInput) enabledInput.checked = data.enabled !== false;
        if (status) {
            const state = data.enabled === false ? '【已关闭】' : '';
            status.textContent = data.password_set
                ? `${state}当前已配置授权码（不回显；留空保持不变）。接收方邮箱在各业务处配置（如 Redmine 用户资料、运维告警 email_to）。`
                : `${state}尚未配置授权码。接收方邮箱在各业务处配置（如 Redmine 用户资料、运维告警 email_to）。`;
        }
    } catch (error) {
        if (status) status.textContent = '读取邮件发送方配置失败: ' + error.message;
    }
}

async function saveEmailSenderConfig() {
    const host = document.getElementById('config-email-smtp-host').value.trim();
    const password = document.getElementById('config-email-password').value;
    const enabledInput = document.getElementById('config-email-enabled');
    const enabled = enabledInput ? enabledInput.checked : undefined;
    const hostUnchanged = !host;
    // 主机为空且开关未变视为不修改邮件配置（用户可能只想改系统配置的其它部分）。
    if (hostUnchanged && enabled === undefined) return;
    const payload = {
        smtp_host: host,
        smtp_port: parseInt(document.getElementById('config-email-smtp-port').value, 10) || 465,
        username: document.getElementById('config-email-username').value.trim(),
        from_addr: document.getElementById('config-email-from-addr').value.trim(),
        password: password ? password : '***',
    };
    if (enabled !== undefined) payload.enabled = enabled;
    await apiCall('/api/email/smtp', 'POST', payload);
}

// 配置模态内动态渲染控件的事件绑定（CSP 收紧前置：去掉 inline handler）。
function _bindConfigModalHandlers(scope) {
    const outboxRefresh = scope.querySelector('#email-outbox-refresh-btn');
    if (outboxRefresh) {
        outboxRefresh.addEventListener('click', loadEmailOutbox);
    }
    const outboxChannel = scope.querySelector('#email-outbox-channel');
    if (outboxChannel) outboxChannel.addEventListener('change', loadEmailOutbox);
    const outboxSent = scope.querySelector('#email-outbox-sent');
    if (outboxSent) outboxSent.addEventListener('change', loadEmailOutbox);
    const form = scope.querySelector('#test-control-config-form');
    if (form) {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            saveConfig();
        });
    }
    const addCredBtn = scope.querySelector('#client-cred-add-btn');
    if (addCredBtn) addCredBtn.addEventListener('click', addClientCredential);
    const addRouteBtn = scope.querySelector('#static-route-add-btn');
    if (addRouteBtn) addRouteBtn.addEventListener('click', addStaticRouteRow);
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
                <button class="btn-xxs" type="button" data-action="delete-static-route">删除</button>
            </td>
        </tr>
    `).join('');
    tbody.querySelectorAll('[data-action="delete-static-route"]').forEach((btn) => {
        btn.addEventListener('click', () => deleteStaticRouteRow(btn));
    });
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
        showToast('网关格式应为 IP 地址，例如 192.0.2.1', 'warning');
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
        if (id.startsWith('ai-analysis-modal-')) {
            // ModalManager.close 同时负责隐藏（display/show/aria）与清理
            // Esc 监听器；这里只补一个延迟 DOM 移除以等动画完成。
            ModalManager.close(id);
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
    // /api/config/update 只接受运行时字段（client_hosts / local_server）。
    // 静态部署拓扑（用户/主机/路径/VID:PID/SSH 密码）按 ADR 0007 存放在
    // configs/local/config.json 与环境变量，Web 端不再提交。
    const config = {
        local_server: document.getElementById('config-local-server').value,
    };

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

        // 邮件发送方独立保存（admin API），失败不阻塞主配置但需提示。
        try {
            await saveEmailSenderConfig();
        } catch (emailError) {
            addLogEntry('邮件发送方保存失败: ' + emailError.message, 'error');
            showToast('邮件发送方保存失败: ' + emailError.message, 'error');
        }

        // Reload page to update config values
        setTimeout(() => location.reload(), 500);
    } catch (error) {
        addLogEntry('保存配置失败: ' + error.message, 'error');
        showToast('保存失败: ' + error.message, 'error');
    }
}
