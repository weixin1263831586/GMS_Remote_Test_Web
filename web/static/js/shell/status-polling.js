// Shell 模块：状态轮询与初始状态检查（从 navigation.js 第二轮拆分）。
// 依赖 api.js 的 apiCall 与 shell/logging.js 的日志函数；均为全局函数。

// ==================== Status Polling ====================
function startStatusPolling() {
    stopTestStatusPolling();
    // 轮询状态和日志
    let shownPyudevWarning = false;  // 标记是否已显示过 pyudev 警告
    let pollInterval = 2000;  // 初始轮询间隔：2秒
    const maxPollInterval = 30000;  // 最大轮询间隔：30秒
    let pollTimer = null;
    let pollRunning = false;
    let pollRequested = false;
    let stopped = false;
    // WebSocket 是实时日志主通道，但 client_id 不一致或推送丢失时它会静默丢日志。
    // wsLogStallTicks 检测"服务端日志在涨、本地却没收到"的停滞后回退到增量拉取。
    // 必须使用全局 state.wsLogStallTicks，WebSocket onmessage 才能正确重置计数。
    let lastSeenServerLogCount = 0; // 最近一次观测到的服务端日志总数

    const schedulePoll = delay => {
        if (stopped) return;
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(() => {
            pollTimer = null;
            void pollStatus();
        }, delay);
    };

    const pollStatus = async () => {
        if (stopped) return;
        if (pollRunning) {
            pollRequested = true;
            return;
        }
        pollRunning = true;
        try {
            if (state.clusterJobId) {
                const jobId = encodeURIComponent(state.clusterJobId);
                // per-job 游标：切 A→B→A 时旧 job 的日志不会
                // 重复追加（每个 job 记住自己的 sequence）。
                state.clusterEventSequenceByJob = state.clusterEventSequenceByJob || {};
                const jobCursor = state.clusterEventSequenceByJob[state.clusterJobId] ?? -1;
                const [jobResponse, eventResponse] = await Promise.all([
                    apiCall(`/api/cluster/jobs/${jobId}`, 'GET', null, {background: true}),
                    apiCall(`/api/cluster/jobs/${jobId}/events?after=${encodeURIComponent(String(jobCursor))}&limit=1000`, 'GET', null, {background: true})
                ]);
                const job = jobResponse.job;
                // 轮询只更新 job/attempt 元数据，不覆盖用户手动选择的 worker。
                // 否则正在运行的旧任务会反复把 worker_id 刷回它分配的主机，
                // 导致用户切换主机后立刻被还原。
                window.GmsWorkspace?.update({
                    cluster_job_id: job.id || state.clusterJobId,
                    attempt_id: job.current_attempt_id || ''
                }, {source: 'test-poll'});
                const currentSequence = jobCursor;
                const events = (eventResponse.events || []).filter(
                    event => Number(event.sequence) > currentSequence
                );
                const eventWorkerId = job.assigned_worker_id || workspaceWorkerId();
                events.forEach(event => addNormalizedLogEntry({
                    message: event.message,
                    type: event.level === 'error' ? 'error' : 'info',
                    source: ['stdout', 'stderr'].includes(event.source) ? 'module' : undefined,
                    worker_id: eventWorkerId,
                    job_id: String(event.job_id || state.clusterJobId || '')
                }));
                if (events.length) {
                    const maxSequence = Math.max(...events.map(event => Number(event.sequence)));
                    state.clusterEventSequenceByJob[state.clusterJobId] = maxSequence;
                    state.clusterEventSequence = maxSequence;
                }
                const active = ['created', 'queued', 'leasing', 'assigned', 'dispatching', 'running', 'stopping', 'collecting', 'worker_lost'].includes(job.status);

                // 只在 job 属于当前选中主机时才更新测试状态。
                // 用户可能已切换到另一台主机：旧 job 继续在后端跑，但 UI
                // 不应把当前主机显示为"测试中"。
                const jobBelongsToCurrentWorker = !job.assigned_worker_id
                    || job.assigned_worker_id === workspaceWorkerId();
                if (jobBelongsToCurrentWorker) {
                    state.testStopping = job.status === 'stopping';
                    state.testing = active;
                    updateTestToggleButton(active);
                }
                if (!active) {
                    const level = job.status === 'completed' ? 'success' : 'error';
                    addLogEntry(`分布式测试 ${job.status}${job.error ? `: ${job.error}` : ''}`, level);
                    showToast(`分布式测试${job.status === 'completed' ? '完成' : '结束'}: ${job.status}`, level);
                    state.clusterJobId = '';
                    state.testStopping = false;
                    resetClusterEventCursor();
                    sessionStorage.removeItem('active_cluster_job');
                    window.GmsWorkspace?.update({
                        cluster_job_id: '',
                        attempt_id: '',
                        report_id: `cluster-${job.id}`,
                        report_timestamp: `cluster-${job.id}`,
                        origin_page: 'test'
                    }, {source: 'test-complete'});
                    loadDevices(true).catch(() => {});
                }
                pollInterval = active ? 1000 : 3000;
                return;
            }
            // 检查是否有 WebSocket 连接
            const hasRealtimeConnection = state.websocket && state.websocket.readyState === WebSocket.OPEN;

            // WebSocket 是实时主通道：连接正常时绝不拉增量日志，否则会与 WebSocket
            // 推送的同一批日志重复显示（两者共用 state.lastLogCount，竞态必现重复）。
            // 但若服务端日志总数持续增长而本地 lastLogCount 不动（WebSocket 推送丢失或
            // client_id 不一致），则回退到 since 增量兜底，避免"测试在跑却看不到日志"。
            let shouldFetchLogs = !hasRealtimeConnection;
            if (hasRealtimeConnection && state.testing && state.wsLogStallTicks >= 2) {
                shouldFetchLogs = true;
            }
            const statusUrl = shouldFetchLogs
                ? `/api/test/status?since=${encodeURIComponent(String(state.lastLogCount || 0))}`
                : '/api/test/status?logs=false';
            const status = await apiCall(statusUrl, 'GET', null, {background: true});

            // Durable jobs survive a Controller restart and do not depend on
            // sessionStorage.  Recover the newest active job for the selected
            // Worker when a tab or workspace has lost its current job id.
            const activeJobs = Array.isArray(status.active_jobs) ? status.active_jobs : [];
            if (!state.clusterJobId && activeJobs.length) {
                // 只恢复属于当前选中主机的活跃任务，不把用户切到别的 worker。
                const recoveredJob = activeJobs.find(job => job.worker_id === workspaceWorkerId());
                if (recoveredJob) {
                    state.clusterJobId = recoveredJob.id;
                    resetClusterEventCursor();
                    state.testStopping = recoveredJob.status === 'stopping';
                    sessionStorage.setItem('active_cluster_job', recoveredJob.id);
                    window.GmsWorkspace?.update({
                        cluster_job_id: recoveredJob.id,
                        attempt_id: recoveredJob.attempt_id || ''
                    }, {source: 'test-durable-recovery'});
                    pollRequested = true;
                    return;
                }
            }

            // 检测 WebSocket 日志停滞：服务端 log_count 在涨、本地却没有跟进时累计计数。
            if (typeof status.log_count === 'number' && hasRealtimeConnection && state.testing) {
                if (status.log_count > (state.lastLogCount || 0)) {
                    state.wsLogStallTicks += 1;
                } else {
                    state.wsLogStallTicks = 0;
                }
                lastSeenServerLogCount = status.log_count;
            }

            // 检查 USB 监控器状态并提示（仅显示一次）
            if (!shownPyudevWarning && status.usb_monitor) {
                const { mode, running, pyudev_available } = status.usb_monitor;
                if (running && mode === 'polling' && !pyudev_available) {
                    shownPyudevWarning = true;
                    const message = '💡 提示：安装 pyudev 可获得更好的USB监控性能（实时响应，低CPU占用）\n' +
                                   '安装方式：重新运行一键安装脚本即可自动安装\n' +
                                   '或手动执行：cd /opt/gms-remote-test/web_app && .venv/bin/pip install pyudev\n' +
                                   '安装后需重启服务：sudo systemctl restart gms-web-app';
                    addLogEntry(message, 'warning');

                    // 也可以在页面显示一次提示
                    if (!localStorage.getItem('pyudev_warning_shown')) {
                        showToast('建议安装 pyudev 以提升性能', 'info');
                        localStorage.setItem('pyudev_warning_shown', 'true');
                    }
                }
            }

            // 更新测试状态按钮
            // status.running 和 active_jobs 是所有主机的全局状态。
            // 只根据当前选中主机的活跃 job 来决定测试状态，避免 A 主机的
            // 测试导致切换到 B 主机后仍显示"测试中"。
            const currentWorkerActiveJobs = activeJobs.filter(j => j.worker_id === workspaceWorkerId());
            const currentWorkerRunning = currentWorkerActiveJobs.length > 0;
            if (currentWorkerRunning && !state.testing) {
                state.testing = true;
                updateTestToggleButton(true);
            } else if (!currentWorkerRunning && state.testing) {
                state.testing = false;
                updateTestToggleButton(false);
            }

            // Update VPN status
            if (status.vpn_connected !== undefined) {
                updateVpnStatus(status.vpn_connected);
            }

            if (status.logs && status.logs.length > 0) {
                status.logs.forEach(addNormalizedLogEntry);
                state.lastLogCount = status.log_count || (state.lastLogCount + status.logs.length);
                // 增量拉取补回日志后重置停滞计数。
                state.wsLogStallTicks = 0;
            } else if (typeof status.log_count === 'number' && shouldFetchLogs) {
                state.lastLogCount = Math.max(state.lastLogCount || 0, status.log_count);
            }

            // 动态调整轮询间隔：如果测试正在运行，使用快速轮询；否则退避
            // Use exponential backoff when no changes detected
            if (currentWorkerRunning) {
                pollInterval = 2000;  // 测试运行时：2秒
            } else {
                // If nothing changed since last poll, increase backoff faster
                const stateChanged = (currentWorkerRunning !== state.testing) ||
                                     (status.vpn_connected !== undefined && status.vpn_connected !== state.vpnConnected);
                if (stateChanged) {
                    pollInterval = 2000;  // Reset to fast polling on state change
                } else {
                    pollInterval = Math.min(pollInterval * 1.5, maxPollInterval);  // 测试未运行时：逐渐增加到30秒
                }
            }

        } catch (error) {
            console.error('Status polling error:', error);
            // 失败也要退避：会话失效（401）时轮询曾以 2 秒频率无限重试，
            // 单个后台标签页一天可产生数万次请求，持续占用本机连接。
            const authLost = error?.status === 401 || error?.status === 403;
            pollInterval = Math.min(
                Math.max(pollInterval, 2000) * (authLost ? 5 : 1.5),
                maxPollInterval
            );
        } finally {
            pollRunning = false;
            const nextDelay = pollRequested ? 0 : pollInterval;
            pollRequested = false;
            schedulePoll(nextDelay);
        }
    };

    stopTestStatusPolling = () => {
        stopped = true;
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
    };
    wakeTestStatusPolling = () => {
        if (stopped) return;
        pollInterval = 250;
        if (pollRunning) {
            pollRequested = true;
            return;
        }
        schedulePoll(0);
    };

    wakeTestStatusPolling();
}

async function checkInitialTestStatus() {
    try {
        const workspace = await (window.GmsWorkspace?.ready || Promise.resolve({}));
        const savedClusterJob = sessionStorage.getItem('active_cluster_job') || workspace.cluster_job_id || '';
        if (savedClusterJob) {
            if (state.clusterJobId !== savedClusterJob) {
                state.clusterJobId = savedClusterJob;
                resetClusterEventCursor();
            }
            let response;
            try {
                response = await apiCall(`/api/cluster/jobs/${encodeURIComponent(savedClusterJob)}`, 'GET', null, {background: true});
            } catch (fetchError) {
                // 网络错误或 job 不存在：清理残留状态，不阻塞页面初始化。
                debugLog('[Init] Failed to fetch cluster job, clearing stale state:', fetchError);
                sessionStorage.removeItem('active_cluster_job');
                state.clusterJobId = '';
                state.testing = false;
                state.testStopping = false;
                updateTestToggleButton(false);
                window.GmsWorkspace?.update(
                    {cluster_job_id: '', attempt_id: ''},
                    {source: 'test-recovery-failed'}
                );
                return;
            }
            const jobStatus = response?.job?.status;
            if (!jobStatus) {
                // Job 不存在或响应异常：清理残留状态。
                sessionStorage.removeItem('active_cluster_job');
                state.clusterJobId = '';
                state.testing = false;
                state.testStopping = false;
                updateTestToggleButton(false);
                window.GmsWorkspace?.update(
                    {cluster_job_id: '', attempt_id: ''},
                    {source: 'test-recovery-missing'}
                );
                return;
            }
            const active = ['created', 'queued', 'leasing', 'assigned', 'dispatching', 'running', 'stopping', 'collecting', 'worker_lost'].includes(jobStatus);
            // 只在 job 属于当前选中主机时才显示测试中状态。
            const jobWorkerId = response?.job?.assigned_worker_id || '';
            const jobBelongsToCurrentWorker = !jobWorkerId
                || jobWorkerId === workspaceWorkerId();
            state.testStopping = jobStatus === 'stopping' && jobBelongsToCurrentWorker;
            state.testing = active && jobBelongsToCurrentWorker;
            updateTestToggleButton(state.testing);
            if (active) {
                wakeTestStatusPolling();
                return;
            }
            sessionStorage.removeItem('active_cluster_job');
            state.clusterJobId = '';
            window.GmsWorkspace?.update(
                {cluster_job_id: '', attempt_id: ''},
                {source: 'test-recovery-terminal'}
            );
        }
        const status = await apiCall('/api/test/status');
        const activeJobs = Array.isArray(status.active_jobs) ? status.active_jobs : [];
        if (activeJobs.length) {
            // 只恢复属于当前选中主机的活跃任务，不把用户切到别的 worker。
            const recoveredJob = activeJobs.find(job => job.worker_id === workspaceWorkerId());
            if (recoveredJob) {
                state.clusterJobId = recoveredJob.id;
                resetClusterEventCursor();
                state.testStopping = recoveredJob.status === 'stopping';
                state.testing = true;
                sessionStorage.setItem('active_cluster_job', recoveredJob.id);
                window.GmsWorkspace?.update({
                    cluster_job_id: recoveredJob.id,
                    attempt_id: recoveredJob.attempt_id || ''
                }, {source: 'test-initial-durable-recovery'});
                updateTestToggleButton(true);
                wakeTestStatusPolling();
                return;
            }
        }
        // 只根据当前选中主机的活跃 job 来判断测试状态。
        const initialWorkerActiveJobs = activeJobs.filter(j => j.worker_id === workspaceWorkerId());
        const initialWorkerRunning = initialWorkerActiveJobs.length > 0;
        state.testing = initialWorkerRunning;
        state.testStopping = false;
        updateTestToggleButton(initialWorkerRunning);
        if (initialWorkerRunning) wakeTestStatusPolling();
        // 重置停滞计数：页面刚加载，WebSocket 可能尚未就绪或尚未投递日志。
        state.wsLogStallTicks = 0;

        // 页面刷新时加载历史日志（限制最近100条，避免卡顿）
        if (status.logs && status.logs.length > 0) {
            const recentLogs = status.logs.slice(-getLogDisplayLimit());

            // 用 DocumentFragment 同步构建再一次性替换，避免先 innerHTML=''
            // 清空再 rAF 异步回填造成的一帧空白。
            const buckets = { system: [], module: [] };
            recentLogs.forEach(rawLog => {
                const entry = normalizeLogEntry(rawLog);
                (buckets[entry.source === 'module' ? 'module' : 'system']).push(entry);
            });

            for (const src of ['system', 'module']) {
                const container = getLogContainer(src);
                if (!container) continue;
                const fragment = document.createDocumentFragment();
                buckets[src].forEach(({ message, type, source }) => {
                    const div = document.createElement('div');
                    div.className = `log-entry log-${type}`;
                    div.textContent = `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ${message}`;
                    fragment.appendChild(div);
                });
                container.innerHTML = '';
                container.appendChild(fragment);
            }

            const activeOut = getLogContainer(state.currentLogTab || 'system');
            if (activeOut) activeOut.scrollTop = activeOut.scrollHeight;

            state.lastLogCount = status.log_count || status.logs.length;
        } else {
            state.lastLogCount = typeof status.log_count === 'number' ? status.log_count : 0;
        }
    } catch (error) {
        console.error('Failed to check initial test status:', error);
        state.lastLogCount = 0;
    }
}

