// Shell 模块：系统/测试日志面板（从 navigation.js 第二轮拆分，2026-08 审核）。
// 依赖 state.js 的 state/debugLog；函数保持全局作用域，供 shell 内联与 pages 使用。

// ==================== Logging ====================
// 批量合并日志，减少 DOM 更新。
const _logQueue = [];
let _logFlushScheduled = false;

// 返回系统或模块日志容器。
function getLogContainer(source = 'system') {
    return document.getElementById(`${source === 'module' ? 'module' : 'system'}-log-output`);
}

const MODULE_LOG_PATTERNS = [
    /\b(?:CTS|VTS|GTS|STS|Tradefed|TradeFed|Compatibility Console|Invocation)\b/,
    /\b(?:ModuleListener|PrettyTestEventLogger|TestRunner|TestInvocation|ITestInvocationListener)\b/,
    /\b(?:testRunStarted|testRunEnded|testStarted|testEnded|testFailed|testIgnored|IGNORED|ASSUMPTION_FAILURE)\b/,
    /\b(?:PASSED|FAILED)\b/,
    /\[[0-9]+\/[0-9]+\]\s+\S+\s+\S+#\S+/
];

function inferLogSource(message, explicitSource) {
    if (explicitSource === 'module' || explicitSource === 'system') {
        return explicitSource;
    }

    const text = String(message || '');
    return MODULE_LOG_PATTERNS.some(pattern => pattern.test(text)) ? 'module' : 'system';
}

function normalizeLogEntry(log) {
    const isObject = log && typeof log === 'object';
    const message = isObject
        ? (log.msg || log.message || log.log || '')
        : String(log || '');
    const cleanedMessage = String(message).replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '');

    return {
        message: cleanedMessage,
        type: isObject ? (log.type || log.log_type || 'info') : 'info',
        source: inferLogSource(cleanedMessage, isObject ? log.source : undefined),
        // 日志条目携带 worker/job scope，切主机按过滤条件
        // 显示，而不是共享一锅日志造成串台。
        worker_id: isObject ? String(log.worker_id || '') : '',
        job_id: isObject ? String(log.job_id || '') : ''
    };
}

function addNormalizedLogEntry(log) {
    const entry = normalizeLogEntry(log);
    addLogEntry(entry.message, entry.type, true, entry.source, entry.worker_id, entry.job_id);
}

function getLogDisplayLimit() {
    return parseInt(localStorage.getItem('gms-log-history-limit')) || 100;
}
function getLogMaxEntries() {
    return parseInt(localStorage.getItem('gms-log-max-entries')) || 1000;
}

// 集群测试日志游标统一重置：全局游标与 per-job 游标（按 job 记忆，
// 避免 A→B→A 重复追加）必须同步清理，否则残留游标会吞掉后续日志。
function resetClusterEventCursor() {
    state.clusterEventSequence = -1;
    if (state.clusterEventSequenceByJob) delete state.clusterEventSequenceByJob[state.clusterJobId];
}

function addLogEntry(message, type = 'info', showTimestamp = true, source = 'system', workerId = '', jobId = '') {
    // Queue the log entry
    _logQueue.push({
        message,
        type,
        showTimestamp,
        source: source === 'module' ? 'module' : 'system',
        workerId: String(workerId || ''),
        jobId: String(jobId || ''),
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false })
    });

    // 限制队列大小，避免 WebSocket 突发日志占满内存。
    if (_logQueue.length > 500) _logQueue.splice(0, _logQueue.length - 500);

    // Schedule a flush if not already scheduled
    if (!_logFlushScheduled) {
        _logFlushScheduled = true;
        requestAnimationFrame(flushLogQueue);
    }
}

// 针对特定 Worker 的操作日志统一入口：强制携带 worker scope，
// 避免调用方只把 Worker 写进文字而漏传 metadata 导致跨主机串台。
function addWorkerLog(workerId, message, type = 'info', source = 'system', jobId = '') {
    addLogEntry(message, type, true, source, workerId, jobId);
}

// 日志 scope 过滤。scope 信息随条目保存（data-* 属性），
// 切换测试主机时只需换过滤条件，不删除历史。
// - 无 scope 条目（本地 Controller 操作）：任何 Worker 下都显示；
// - worker scope 条目：只在对应 Worker（或未标记时）显示；
// - job scope 条目（module 日志）：跟随 job 所属 Worker 显示。
function _entryMatchesScope(entry) {
    const currentWorker = window.workspaceWorkerId ? window.workspaceWorkerId() : '';
    if (!entry.workerId && !entry.jobId) return true;
    return !entry.workerId || !currentWorker || entry.workerId === currentWorker;
}

function applyLogScopeFilter() {
    for (const src of ['system', 'module']) {
        const logOutput = getLogContainer(src);
        if (!logOutput) continue;
        let shouldFollow = isLogScrolledNearBottom(logOutput);
        let visible = 0;
        for (const node of logOutput.children) {
            if (node.nodeType !== Node.ELEMENT_NODE) continue;
            const matches = !node.dataset.workerId
                || !window.workspaceWorkerId
                || node.dataset.workerId === window.workspaceWorkerId();
            node.style.display = matches ? '' : 'none';
            if (matches) visible++;
        }
        if (shouldFollow) logOutput.scrollTop = logOutput.scrollHeight;
    }
}

function flushLogQueue() {
    _logFlushScheduled = false;

    // Take all queued entries
    const entries = _logQueue.splice(0, _logQueue.length);
    if (entries.length === 0) return;

    // Route each entry to its log container by source
    const maxLogs = getLogMaxEntries();
    const buckets = { system: [], module: [] };
    entries.forEach(entry => (buckets[entry.source] || buckets.system).push(entry));

    for (const src of ['system', 'module']) {
        const bucket = buckets[src];
        if (!bucket.length) continue;
        const logOutput = getLogContainer(src);
        if (!logOutput) continue;
        const shouldFollow = isLogScrolledNearBottom(logOutput);

        // Use DocumentFragment for batch DOM insertion
        const fragment = document.createDocumentFragment();
        bucket.forEach(({ message, type, timestamp, showTimestamp, workerId, jobId }) => {
            const logEntry = document.createElement('div');
            logEntry.className = `log-entry log-${type}`;
            logEntry.textContent = showTimestamp ? `[${timestamp}] ${message}` : message;
            if (workerId) logEntry.dataset.workerId = workerId;
            if (jobId) logEntry.dataset.jobId = jobId;
            if (!_entryMatchesScope({ workerId, jobId })) {
                logEntry.style.display = 'none';
            }
            fragment.appendChild(logEntry);
        });

        logOutput.appendChild(fragment);

        // Batch trim old log entries (keep max 500 per container)
        if (logOutput.children.length > maxLogs) {
            const removeCount = logOutput.children.length - maxLogs;
            const range = document.createRange();
            range.setStartBefore(logOutput.firstChild);
            range.setEndBefore(logOutput.children[removeCount]);
            range.deleteContents();
        }

        if (shouldFollow) {
            logOutput.scrollTop = logOutput.scrollHeight;
        }
    }
}

// 清空指定 Worker scope 的日志条目。
// 日志面板是跨主机共用的（隐藏而非删除），开始测试/清除日志只能清当前
// Worker 的条目；无 scope 的全局条目（如登录/平台事件）也一并保留，
// 避免其他 Worker 的隐藏历史被误删。
function clearWorkerLogs(workerId = '') {
    const scope = String(workerId || (window.workspaceWorkerId ? window.workspaceWorkerId() : ''));
    for (const src of ['system', 'module']) {
        const logOutput = getLogContainer(src);
        if (!logOutput) continue;
        for (const node of Array.from(logOutput.children)) {
            if (node.nodeType !== Node.ELEMENT_NODE) continue;
            // 只删除属于当前 Worker scope 的条目；无 scope（全局/Controller/
            // 登录信息等）的条目保留——"清当前 Worker 日志"不应误删平台日志。
            if (node.dataset.workerId === scope) {
                node.remove();
            }
        }
    }
    state.lastLogCount = 0;
    state.wsLogStallTicks = 0;
}

function isLogScrolledNearBottom(logOutput) {
    if (!logOutput) return true;
    const distance = logOutput.scrollHeight - logOutput.clientHeight - logOutput.scrollTop;
    return distance <= 24;
}

// 切换系统操作和模块测试日志。
function switchLogTab(tabName) {
    const target = tabName === 'module' ? 'module' : 'system';
    state.currentLogTab = target;
    sessionStorage.setItem('gms_test_log_tab', target);

    document.querySelectorAll('.log-tab-btn').forEach(btn => {
        const selected = btn.dataset.logTab === target;
        btn.classList.toggle('active', selected);
        btn.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    document.querySelectorAll('.log-tab-content').forEach(panel => {
        panel.classList.toggle('active', panel.id === `log-tab-${target}`);
    });

    const out = getLogContainer(target);
    if (out) out.scrollTop = out.scrollHeight;
}

// 用户主动发起设备、烧写、VNC、VPN 或上传操作时显示系统日志。
// 只绑定操作按钮点击，避免后台系统消息在测试运行时抢走“测试日志”页签。
document.addEventListener('click', (event) => {
    const button = event.target?.closest?.(
        '#page-test [data-operation-log-tab="system"] button'
    );
    if (!button || button.disabled) return;
    switchLogTab('system');
});

// state.js 已从当前浏览器标签页恢复选择；DOM 解析完成后在首次绘制前应用。
switchLogTab(state.currentLogTab || 'system');

// 更新进度条 - 使用固件上传的进度条
function updateProgressBar(percentage, message = '', title = '进度') {
    debugLog('[Progress] updateProgressBar called:', percentage, message, title);

    const progressContainer = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-fill');
    const progressInfo = document.getElementById('progress-info');

    if (!progressContainer || !progressFill || !progressInfo) {
        console.warn('[Progress] Progress bar elements not found');
        return;
    }

    // 显示进度条
    progressContainer.style.display = 'flex';

    // 更新进度
    progressFill.style.width = `${percentage}%`;

    // 显示标题和百分比在进度条右侧
    progressInfo.textContent = `${title} ${percentage.toFixed(1)}%`;

    // 如果有消息，显示在日志中
    if (message) {
        addLogEntry(message, 'info');
    }

    debugLog('[Progress] Updated to:', percentage);

    // 如果进度完成，3秒后隐藏进度条
    if (percentage >= 100) {
        setTimeout(() => {
            progressContainer.style.display = 'none';
            progressFill.style.width = '0%';
            progressInfo.textContent = '';
            state.currentBurningProgress = 0;  // 重置进度状态
        }, 3000);
    }
}

// 上传文件进度
