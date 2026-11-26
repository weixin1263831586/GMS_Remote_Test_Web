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

    // 执行请求只允许携带经过最新库存与状态校验的执行集（审核 P2）：
    // state.selectedDevices 可能还包含占用/失联设备的恢复记忆勾选。
    const devices = selectedTestDeviceIds();
    if (!devices.length) {
        showToast('所选设备当前不可执行测试，请重新选择', 'warning');
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
            devices,
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
                device_ids: devices,
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
        btn.textContent = '停止中';
        btn.className = 'btn-danger btn-lg';
    } else if (state.testStarting) {
        btn.disabled = true;
        btn.textContent = '启动中';
        btn.className = 'btn-primary btn-lg';
    } else if (state.testing) {
        btn.disabled = false;
        btn.textContent = '停止测试';
        btn.className = 'btn-danger btn-lg';
    } else {
        btn.disabled = false;
        btn.textContent = '开始测试';
        btn.className = 'btn-primary btn-lg';
    }

    // 禁用/启用测试配置控件（统一契约：data-test-config-control，
    // 覆盖测试类型/模块/用例/套件/报告输入及“📁 选择报告”浏览按钮）。
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

function closeModal(modalId) {
    const id = modalId || 'sidebar-visibility-modal';
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

