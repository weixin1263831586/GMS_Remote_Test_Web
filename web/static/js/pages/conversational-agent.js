let agentSessionId = localStorage.getItem('gms_agent_session_id') || '';
let agentPollTimer = null;
let agentInitialized = false;
let agentInputHistory = JSON.parse(localStorage.getItem('gms_agent_input_history') || '[]');
let agentHistoryIndex = -1;  // -1 = not browsing history
let agentModelStatusLoaded = false;
let agentModelStatusFlight = null;
let agentElevationFlight = null;
let agentRetryId = '';
let agentCurrentSession = null;
let agentSessionDirectory = [];
let agentDirectoryFlight = null;
let agentDirectoryStateKey = '';
const agentHandledAutoOpenMessageIds = new Set();

function agentModelStatusText(status) {
    const providers = status?.providers || [];
    const providerName = status?.local_provider || status?.primary_provider || '';
    const provider = providers.find(item => item.provider === providerName) || providers[0];
    if (!provider) return {text: '分析模型：未配置', state: 'unavailable', title: '未配置可用的 AI 分析模型'};
    const label = provider.local ? '本地模型' : '分析模型';
    if (provider.available === true) {
        const latency = Number.isFinite(provider.latency_ms) ? ` · ${provider.latency_ms}ms` : '';
        return {text: `${label}：可用${latency}`, state: 'available', title: `${provider.name} / ${provider.model}`};
    }
    if (provider.state === 'credential_missing') {
        return {text: `${label}：缺少密钥`, state: 'credential_missing', title: provider.error || '请配置对应 API key 环境变量'};
    }
    if (provider.available === false || ['disabled', 'incomplete', 'unavailable'].includes(provider.state)) {
        return {text: `${label}：不可用`, state: 'unavailable', title: provider.error || '模型配置不完整或探测失败'};
    }
    return {text: `${label}：待检测`, state: 'ready_to_probe', title: `${provider.name} / ${provider.model}；点击“检测”执行一次最小推理`};
}

function loadAgentModelStatus(probe = false, quiet = false) {
    if (agentModelStatusFlight) return agentModelStatusFlight;
    agentModelStatusFlight = fetchAgentModelStatus(probe, quiet).finally(() => {
        agentModelStatusFlight = null;
    });
    return agentModelStatusFlight;
}

async function fetchAgentModelStatus(probe, quiet) {
    const label = $('agent-model-status');
    const button = $('agent-model-check');
    if (!label || !button) return;
    if (probe) {
        button.disabled = true;
        button.textContent = '检测中';
        label.textContent = '分析模型：检测中…';
        label.dataset.state = 'loading';
    }
    try {
        const response = await fetch(`/api/config/ai${probe ? '?probe=true' : ''}`);
        const result = await response.json().catch(() => ({}));
        if (!response.ok || !result.success) throw new Error(result.error || result.message || `HTTP ${response.status}`);
        const view = agentModelStatusText(result.data?.status);
        label.textContent = view.text;
        label.dataset.state = view.state;
        label.title = `${view.title}。该模型用于报告/知识分析；对话引擎状态单独显示。`;
        if (probe && !quiet) showToast(view.state === 'available' ? 'AI 分析模型可用' : view.text, view.state === 'available' ? 'success' : 'warning');
    } catch (error) {
        label.textContent = '分析模型：不可用';
        label.dataset.state = 'unavailable';
        label.title = error.message;
        if (probe && !quiet) showToast(`模型检测失败: ${error.message}`, 'warning');
    } finally {
        agentModelStatusLoaded = true;
        button.disabled = false;
        button.textContent = '检测';
    }
}

function getAgentWorkspaceContext() {
    const context = window.GmsWorkspace?.get?.() || {};
    return {
        scope_mode: context.scope_mode || 'single',
        worker_id: context.worker_id || workspaceLocalWorkerId(),
        device_ids: Array.isArray(context.device_ids) ? context.device_ids : [],
        suite_key: context.suite_key || '',
        suite_path: context.suite_path || '',
        cluster_job_id: context.cluster_job_id || '',
        attempt_id: context.attempt_id || '',
        automation_run_id: context.automation_run_id || '',
        report_id: context.report_id || '',
        report_timestamp: context.report_timestamp || '',
        artifact_id: context.artifact_id || '',
        gerrit_change_id: context.gerrit_change_id || '',
        gerrit_patchset: context.gerrit_patchset || '',
        redmine_issue_id: context.redmine_issue_id || '',
        origin_page: 'agent'
    };
}

function applyAgentSessionWorkspace(session) {
    const context = session?.workspace_context;
    if (!context || typeof context !== 'object') return;
    const current = getAgentWorkspaceContext();
    // 空闲会话仅供展示，不覆盖当前工作区选择。
    const authoritative = ['planning', 'running', 'monitoring', 'analyzing'].includes(session?.status)
        || Boolean(context.cluster_job_id || context.automation_run_id);
    if (authoritative) {
        const changed = Object.keys(context).some(key => JSON.stringify(current[key]) !== JSON.stringify(context[key]));
        if (changed) window.GmsWorkspace?.update(context, {source: 'agent'});
    }
}

function getAgentStatusLabel(status) {
    const labels = {
        idle: '空闲',
        planning: '待确认',
        running: '测试中',
        monitoring: '监控中',
        analyzing: '分析中',
        done: '完成',
        error: '异常',
        cancelled: '已取消',
        awaiting_authorization: '等待授权',
        thinking: '思考中'
    };
    return labels[status] || status || '空闲';
}

function getCurrentPageName() {
    const active = document.querySelector('.page-content.active');
    return active?.id?.replace(/^page-/, '') || '';
}

const agentBeijingTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hourCycle: 'h23',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
});

function formatAgentTime(value) {
    if (!value) return '';
    const raw = String(value).trim();
    // 兼容旧会话保存的北京时间（无时区）；新时间戳始终携带时区。
    const timestamp = /^\d{4}-\d{2}-\d{2}T/.test(raw) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)
        ? `${raw}+08:00` : raw;
    const date = new Date(timestamp);
    return Number.isNaN(date.getTime()) ? '' : agentBeijingTimeFormatter.format(date);
}

function getAgentMessageTone(message) {
    const content = message?.content || '';
    if (message?.kind === 'plan' || /生成执行计划|需要确认|确认后开始/.test(content)) return 'plan';
    if (/失败|异常|没有可用|不能执行|error/i.test(content)) return 'error';
    if (/完成|已启动|已取消|成功/.test(content)) return 'success';
    return '';
}

function renderAgentPlanContent(content) {
    const lines = String(content || '').split('\n').map(line => line.trim()).filter(Boolean);
    const intro = [];
    const fields = [];
    const notes = [];

    lines.forEach(line => {
        const item = line.replace(/^\s*- /, '');
        const idx = item.indexOf(':');
        if (/^\s*- /.test(line) && idx > 0) {
            fields.push([item.slice(0, idx).trim(), item.slice(idx + 1).trim()]);
        } else if (/输入|当前没有|不能执行|存在阻塞|解决后|重新规划/.test(line)) {
            notes.push(line.replace(/^\s+/, ''));
        } else {
            intro.push(line);
        }
    });

    const introHtml = intro.length ? `<div style="margin-bottom: 10px;">${escapeHtml(intro.join('\n'))}</div>` : '';
    const gridHtml = fields.length ? `
        <div class="agent-plan-grid">
            ${fields.map(([key, value]) => `
                <div class="agent-plan-key">${escapeHtml(key)}</div>
                <div class="agent-plan-value">${escapeHtml(value || '-')}</div>
            `).join('')}
        </div>
    ` : '';
    const noteHtml = notes.length ? `<div class="agent-plan-note">${escapeHtml(notes.join('\n'))}</div>` : '';
    return introHtml + gridHtml + noteHtml;
}

function renderAgentMessages(session) {
    const container = $('agent-chat-messages');
    if (!container) return;

    const messages = session?.messages || [];
    if (!messages.length) {
        container.innerHTML = '<div class="agent-chat-empty">可以问：每个页面功能、rk3572设备、最近报告、测试套件、VPN状态；也可以说：跑 CtsWifiTestCases，失败 retry 2 次并分析报告</div>';
        return;
    }

    container.innerHTML = messages.map(message => {
        const isUser = message.role === 'user';
        const data = message.data || {};
        const plan = data.plan || null;
        const kind = message.kind || 'text';
        const reportTimestamp = data.report_timestamp || '';
        const apkTaskId = data.analysis?.apk_source_analysis?.task_id || '';
        const targetPage = data.page || '';
        const quickActions = data.quick_actions || [];
        const tone = getAgentMessageTone(message);
        const roleLabel = isUser ? '你' : 'Agent';
        const isPlanLike = kind === 'plan' || plan;
        let actions = '';

        if (plan && plan.type === 'workflow_draft') {
            const planReady = plan.preflight?.ready && session.status !== 'thinking';
            const blockers = (plan.preflight?.blocks || []).map(item => item.reason || item.stage).join('；')
                || '资源预检未通过，请点击重新规划刷新';
            // 多阶段工作流：重新规划走显式 plan 操作（保留实体并升版本）。
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" data-click="sendAgentMessage" data-a0="false" data-a1="重新规划">重新规划</button>
                    <button class="btn-xs" data-click="sendAgentMessage" data-a0="false" data-a1="取消计划">取消计划</button>
                    <button class="btn-xs" data-click="confirmAgentPlan" ${planReady ? '' : 'disabled'}
                        title="${escapeHtml(session.status === 'thinking' ? 'Agent 正在规划，请稍候' : planReady ? '确认后执行当前计划' : blockers)}">确认执行</button>
                </div>
                ${plan.preflight?.ready ? '' : `<div class="agent-plan-note">暂不可执行：${escapeHtml(blockers)}。解决后点击“重新规划”刷新。</div>`}
            `;
        } else if (plan && session.status === 'planning') {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" data-click="confirmAgentPlan">确认执行</button>
                    <button class="btn-xs" data-click="sendAgentMessage" data-a0="false" data-a1="重新规划">重新规划</button>
                </div>
            `;
        }

        if (reportTimestamp) {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" data-click="openAgentReportAnalysis" data-a0="${escapeJsAttr(reportTimestamp)}">打开报告分析</button>
                    <button class="btn-xs" data-click="switchPage" data-a0="reports" data-a1="null">报告管理</button>
                    ${apkTaskId ? `<button class="btn-xs" data-click="openAgentApkAnalysis" data-a0="${escapeJsAttr(apkTaskId)}">打开APK分析</button>` : ''}
                </div>
            `;
        }

        // Quick actions from response generator
        if (quickActions.length > 0) {
            const actionBtns = quickActions.map(a => {
                if (a.page) {
                    return `<button class="btn-xs" data-click="openAgentPageAction" data-a0="${escapeJsAttr(a.page)}" data-a1="${escapeJsAttr(JSON.stringify(a.params || {}))}">${escapeHtml(a.label)}</button>`;
                } else if (a.action) {
                    const retryDisabled = ['agent_retry', 'agent_elevate_retry'].includes(a.action) && (session.status === 'thinking'
                        || session.status === 'cancelled' || a.params?.retry_id !== session.agent_retry?.id);
                    return `<button class="btn-xs" data-click="sendAgentAction" data-a0="${escapeJsAttr(a.action)}" data-a1="${escapeJsAttr(JSON.stringify(a.params || {}))}" data-a2="${escapeJsAttr(a.label || a.action)}" ${retryDisabled ? 'disabled' : ''}>${escapeHtml(a.label)}</button>`;
                }
                return '';
            }).filter(Boolean).join('');
            if (actionBtns) {
                actions += `<div class="agent-actions">${actionBtns}</div>`;
            }
        }

        // Page navigation button (fallback when no other actions)
        if (!data.auto_open && !reportTimestamp && !quickActions.length && targetPage && targetPage !== 'agent') {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" data-click="switchPage" data-a0="${escapeJsAttr(targetPage)}" data-a1="null">打开页面</button>
                </div>
            `;
        }

        const escapedContent = escapeHtml(message.content || '').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        const contentHtml = (isPlanLike ? renderAgentPlanContent(message.content || '') : escapedContent)
            + (data.question ? renderAgentQuestion(message, session) : '');
        const toneClass = tone ? ` ${tone}` : '';
        const bodyClass = isPlanLike ? 'agent-message-body plan-body' : 'agent-message-body';

        return `
            <div class="agent-message-row ${isUser ? 'user' : 'assistant'}">
                <span class="agent-avatar" aria-hidden="true">${GmsUi.icon(isUser ? 'user' : 'bot')}</span>
                <div class="agent-message ${isUser ? 'user' : 'assistant'}${toneClass}">
                    <div class="agent-message-header">
                        <span class="agent-role">${roleLabel}</span>
                        <span class="agent-time">${escapeHtml(formatAgentTime(message.created_at))}</span>
                    </div>
                    <div class="${bodyClass}">${contentHtml}${actions}</div>
                </div>
            </div>
        `;
    }).join('');
    container.scrollTop = container.scrollHeight;

    const lastAutoOpen = [...messages].reverse().find(message =>
        message.role !== 'user'
        && message.data?.auto_open
        && message.data?.page
        && !agentHandledAutoOpenMessageIds.has(message.id)
    );
    if (lastAutoOpen) {
        agentHandledAutoOpenMessageIds.add(lastAutoOpen.id);
        if (lastAutoOpen.data.page !== getCurrentPageName()) {
            setTimeout(() => switchPage(lastAutoOpen.data.page, null), 0);
        }
    }
}

// 与 features/automation/ui/state.js 的流水线阶段表保持镜像：Agent 页
// 无法引用 ATS 懒加载帧内的常量，步骤面板独立渲染同一套阶段模型。
const AGENT_PIPELINE_STAGES = [
    'queued', 'jenkins_queued', 'jenkins_building', 'artifact_ready', 'artifact_fetching',
    'waiting_device', 'device_locked', 'flashing', 'flash_completed',
    'aoa_recovering', 'post_flash_verifying', 'flash_verified',
    'testing', 'test_running', 'report_collecting', 'analyzing', 'reporting', 'completed',
];
const AGENT_STAGE_LABELS_ZH = {
    queued: '排队', jenkins_queued: '构建排队', jenkins_building: '编译中',
    artifact_fetching: '固件下载中',
    artifact_ready: '固件就绪', waiting_device: '等待设备', device_locked: '设备已锁',
    flashing: '刷机中', flash_completed: '刷机完成', aoa_recovering: '恢复 USB 调试',
    post_flash_verifying: '固件校验中', flash_verified: '固件已校验', testing: '启动测试',
    test_running: '测试中', report_collecting: '收集报告', analyzing: '分析中',
    reporting: '上报中', completed: '完成',
};
const AGENT_FAILURE_STATUSES = new Set([
    'failed', 'jenkins_failed', 'artifact_missing', 'flash_failed', 'aoa_failed',
    'test_failed', 'analysis_failed', 'reporting_failed',
]);
const AGENT_FAILURE_STAGE_INDEX = {
    jenkins_failed: AGENT_PIPELINE_STAGES.indexOf('jenkins_building'),
    artifact_missing: AGENT_PIPELINE_STAGES.indexOf('artifact_ready'),
    flash_failed: AGENT_PIPELINE_STAGES.indexOf('post_flash_verifying'),
    aoa_failed: AGENT_PIPELINE_STAGES.indexOf('aoa_recovering'),
    test_failed: AGENT_PIPELINE_STAGES.indexOf('test_running'),
    analysis_failed: AGENT_PIPELINE_STAGES.indexOf('analyzing'),
    reporting_failed: AGENT_PIPELINE_STAGES.indexOf('reporting'),
};
const AGENT_RUN_STATUS_LABELS_ZH = {
    ...AGENT_STAGE_LABELS_ZH,
    cancelled: '已取消', failed: '失败', jenkins_failed: '编译失败',
    artifact_missing: '固件缺失', flash_failed: '刷机失败', aoa_failed: 'AOA 恢复失败',
    test_failed: '测试失败', analysis_failed: '分析失败', reporting_failed: '上报失败',
};

function agentRunProgressHtml(runId, progress) {
    const status = String(progress?.status || '');
    if (!status) {
        return `<div class="agent-run-progress agent-run-progress-empty">Run ${escapeHtml(runId)} 已提交，等待状态上报…</div>`;
    }
    const currentStage = String(progress?.current_stage || status);
    const isCompleted = status === 'completed';
    const isCancelled = status === 'cancelled';
    const isFailed = AGENT_FAILURE_STATUSES.has(status);
    // cursor 模型与 ATS 页 renderStageBar 一致：cursor 之前全部 done，
    // cursor 落在当前/失败段，completed 时越界表示全 done。
    let cursor = Math.max(0, AGENT_PIPELINE_STAGES.indexOf(currentStage || status));
    if (isCompleted) cursor = AGENT_PIPELINE_STAGES.length;
    else if (isFailed) cursor = AGENT_FAILURE_STAGE_INDEX[status] ?? cursor;
    const cells = AGENT_PIPELINE_STAGES.map((stage, idx) => {
        let cls = '';
        if (idx < cursor) cls = 'done';
        else if (idx === cursor) cls = isFailed ? 'failed' : (isCompleted ? '' : 'current');
        const label = AGENT_STAGE_LABELS_ZH[stage] || stage;
        return `<span class="stage-cell ${cls}" title="${escapeHtml(label)}（${stage}）"></span>`;
    }).join('');
    const statusLabel = isCompleted ? '已完成'
        : isCancelled ? '已取消'
        : isFailed ? (AGENT_RUN_STATUS_LABELS_ZH[status] || status)
        : `当前阶段：${AGENT_RUN_STATUS_LABELS_ZH[status] || AGENT_RUN_STATUS_LABELS_ZH[currentStage] || status}`;
    return `
        <div class="agent-run-progress" data-run="${escapeHtml(runId)}">
            <div class="agent-run-progress-head">
                <span class="agent-run-stage-label">${escapeHtml(statusLabel)}</span>
                <span class="agent-run-stage-time">${escapeHtml(formatAgentTime(progress?.updated_at))}</span>
            </div>
            <div class="agent-run-stage-bar">${cells}</div>
            ${status === 'artifact_fetching' ? agentArtifactDownloadHtml(progress?.artifact_download) : ''}
        </div>`;
}

function agentArtifactDownloadHtml(download) {
    const total = Number(download?.total_bytes);
    const done = Number(download?.downloaded_bytes);
    const speed = Number(download?.bytes_per_second);
    if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(done) || done < 0 || done > total) return '';
    const percent = (done * 100 / total).toFixed(1);
    const rate = Number.isFinite(speed) && speed > 0 ? ` · ${formatBytes(speed)}/s` : '';
    return `<div class="agent-artifact-download">固件下载 ${percent}% · ${formatBytes(done)} / ${formatBytes(total)}${rate}<br><progress max="100" value="${percent}" aria-label="固件下载进度"></progress></div>`;
}

function formatAgentDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return '';
    if (value < 60) return `${Math.round(value)} 秒`;
    const minutes = Math.floor(value / 60);
    const rest = Math.round(value % 60);
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}

function renderAgentSteps(session) {
    const stepsEl = $('agent-steps');
    const statusEl = $('agent-status');
    if (statusEl) statusEl.textContent = getAgentStatusLabel(session?.status);
    if (!stepsEl) return;

    const steps = session?.steps || [];
    if (!steps.length) {
        stepsEl.innerHTML = '<div class="suite-empty">等待任务</div>';
        return;
    }

    const presentationByStatus = {
        done: ['success', 'check', '已完成'],
        running: ['accent', 'clock', '进行中'],
        warning: ['warning', 'warning', '需关注'],
        error: ['danger', 'error', '失败'],
    };

    stepsEl.innerHTML = steps.map(step => {
        const presentation = presentationByStatus[step.status] || ['neutral', 'clock', '待执行'];
        const waitStarted = Date.parse(step.data?.started_at || '');
        const waiting = step.status === 'running' && step.data?.kind === 'agent_model_wait' && Number.isFinite(waitStarted);
        const waitDetail = waiting ? ` · 已等待 ${Math.max(0, Math.floor((Date.now() - waitStarted) / 1000))} 秒` : '';
        // 耗时：终态用后端落盘的 duration_seconds；运行中从 started_at
        // 实时累计（轮询刷新时自动更新）。取不到起点则不显示。
        let durationText = '';
        const startMs = Date.parse(step.started_at || step.created_at || '');
        if (step.status === 'running' && Number.isFinite(startMs)) {
            durationText = `已进行 ${formatAgentDuration((Date.now() - startMs) / 1000)}`;
        } else if (step.duration_seconds !== undefined && step.duration_seconds !== null) {
            durationText = `耗时 ${formatAgentDuration(step.duration_seconds)}`;
        }
        // Run 卡片：内嵌实时阶段条（后端监控循环回写 data.progress），
        // 不再只是一个跳转按钮；跳转保留为次级操作。
        const runId = String(step.data?.automation_run_id || '');
        const runLink = runId ? `
                ${agentRunProgressHtml(runId, step.data?.progress || {
                    status: step.data?.status,
                    error: step.data?.error,
                    updated_at: step.created_at,
                })}
                <div style="margin-top: 6px;">
                    <button class="btn-xs" type="button" data-click="openAgentRunInAts" data-a0="${escapeHtml(runId)}">在 GMS ATS 中查看 Run ${escapeHtml(runId)}</button>
                </div>` : '';
        return `
            <article class="agent-step" data-status="${escapeHtml(step.status || 'pending')}">
                <span class="agent-step-marker" aria-hidden="true">${GmsUi.icon(presentation[1])}</span>
                <div class="agent-step-card">
                    <div class="agent-step-heading">
                        <span class="agent-step-title">${escapeHtml(step.title || '')}</span>
                        ${GmsUi.badge(...presentation)}
                    </div>
                    <div class="agent-step-detail">${escapeHtml((step.detail || '') + waitDetail)}</div>
                    <time class="agent-step-time"><span>${escapeHtml(formatAgentTime(step.created_at))}</span>${durationText ? `<span class="agent-step-duration">${escapeHtml(durationText)}</span>` : ''}</time>
                    ${runLink}
                </div>
            </article>
        `;
    }).join('');
}

function openAgentRunInAts(runId) {
    const target = String(runId || '').trim();
    if (!target) return;
    window.GmsWorkspace?.navigate('automation', {
        automation_run_id: target,
        origin_page: 'agent',
    });
}

function renderAgentSession(session) {
    if (!session) return;
    agentSessionId = session.session_id || agentSessionId;
    agentCurrentSession = session;
    agentRetryId = session.agent_retry?.id || '';
    if (agentSessionId) {
        localStorage.setItem('gms_agent_session_id', agentSessionId);
    }
    renderAgentMessages(session);
    renderAgentSteps(session);
    const engine = $('agent-engine-status');
    if (engine) {
        const backend = session.agent_backend;
        const kkagentState = {ready: '已连接', thinking: '规划中', retrying: '重试中', timeout: '响应超时', failed: '连接异常', awaiting_authorization: '等待授权'};
        const kkagentSeverity = {ready: 'ready', thinking: 'thinking', retrying: 'retrying', timeout: 'timeout', failed: 'failed', awaiting_authorization: 'awaiting_authorization'};
        engine.textContent = backend?.mode === 'kkagent' ? `对话引擎：kkagent ${kkagentState[backend.state] || '连接中'}`
            : backend?.mode === 'rules' ? '对话引擎：规则兜底' : '对话引擎：待连接';
        engine.dataset.state = backend?.mode === 'kkagent' ? (kkagentSeverity[backend.state] || 'thinking')
            : backend?.mode === 'rules' ? 'rules' : 'idle';
        engine.title = backend?.detail || '由 kkagent 规划并选择项目工具，平台负责授权和执行。';
    }
    applyAgentSessionWorkspace(session);

    if (['running', 'monitoring', 'thinking'].includes(session.status)) {
        startAgentPolling();
    } else if (agentPollTimer) {
        stopAgentPolling();
    }
    syncAgentSessionDirectory(session);
}

// ===== 会话目录（多会话管理） =====

function syncAgentSessionDirectory(session) {
    // 轮询回写只在会话/状态变化时刷新目录，避免 3s 轮询放大请求量。
    const key = `${session?.session_id || ''}:${session?.status || ''}`;
    if (key === agentDirectoryStateKey) return;
    agentDirectoryStateKey = key;
    refreshAgentSessionDirectory();
}

async function refreshAgentSessionDirectory() {
    if (agentDirectoryFlight) return agentDirectoryFlight;
    agentDirectoryFlight = (async () => {
        try {
            const response = await fetch('/api/agent/sessions', {cache: 'no-store'});
            const result = await response.json().catch(() => ({}));
            if (response.ok && result.success) {
                agentSessionDirectory = result.data?.sessions || [];
                renderAgentSessionDirectory();
            }
        } catch (error) {
            debugLog('[Agent] session directory fetch failed:', error);
        } finally {
            agentDirectoryFlight = null;
        }
    })();
    return agentDirectoryFlight;
}

function renderAgentSessionDirectory() {
    const listEl = $('agent-session-list');
    if (!listEl) return;
    const keyword = ($('agent-session-search')?.value || '').trim().toLowerCase();
    const items = agentSessionDirectory.filter(item =>
        !keyword || String(item.title || '').toLowerCase().includes(keyword));
    if (!items.length) {
        listEl.innerHTML = `<div class="agent-session-empty">${keyword ? '没有匹配的会话' : '暂无历史会话'}</div>`;
        return;
    }
    listEl.innerHTML = items.map(item => {
        const active = item.session_id === agentSessionId;
        const time = formatAgentTime(item.updated_at) || formatAgentTime(item.created_at);
        return `
            <div class="agent-session-item${active ? ' active' : ''}${item.active ? ' live' : ''}">
                <button type="button" class="agent-session-main" data-click="switchAgentSession"
                    data-a0="${escapeJsAttr(item.session_id)}" title="${escapeHtml(item.title)}">
                    <span class="agent-session-title">${escapeHtml(item.title || '未命名会话')}</span>
                    <span class="agent-session-meta">
                        <span class="agent-session-status">${escapeHtml(item.active ? '运行中' : getAgentStatusLabel(item.status))}</span>
                        <span class="agent-session-time">${escapeHtml(time)}</span>
                    </span>
                </button>
                <button type="button" class="agent-session-delete" data-click="deleteAgentSessionItem"
                    data-a0="${escapeJsAttr(item.session_id)}" title="删除会话" aria-label="删除会话">${GmsUi.icon('trash')}</button>
            </div>
        `;
    }).join('');
}

async function switchAgentSession(sessionId) {
    const target = String(sessionId || '');
    if (!target || target === agentSessionId) return;
    stopAgentPolling();
    agentSessionId = target;
    localStorage.setItem('gms_agent_session_id', agentSessionId);
    clearAgentQuestions();
    agentDirectoryStateKey = '';
    renderAgentSessionDirectory();
    await fetchAgentSession();
    refreshAgentSessionDirectory();
}

async function deleteAgentSessionItem(sessionId) {
    const target = String(sessionId || '');
    if (!target) return;
    const entry = agentSessionDirectory.find(item => item.session_id === target);
    if (entry?.active) {
        showToast('会话有运行中的任务，请先取消 Agent 再删除', 'warning');
        return;
    }
    try {
        const response = await fetch(`/api/agent/sessions/${encodeURIComponent(target)}`, {method: 'DELETE'});
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.success === false) {
            showToast(result.error || result.message || `删除失败 (HTTP ${response.status})`, 'error');
            return;
        }
        if (target === agentSessionId) newAgentSession();
        showToast('会话已删除', 'success');
        refreshAgentSessionDirectory();
    } catch (error) {
        showToast(`删除失败: ${error.message}`, 'error');
    }
}

function exportAgentSessionTranscript() {
    const session = agentCurrentSession;
    const messages = session?.messages || [];
    const steps = session?.steps || [];
    if (!session || (!messages.length && !steps.length)) {
        showToast('当前会话没有可导出的记录', 'warning');
        return;
    }
    const lines = [
        '# GMS 对话Agent 会话记录',
        `会话：${session.session_id}`,
        `导出时间：${new Date().toLocaleString('zh-CN')}`,
        '',
    ];
    if (steps.length) {
        lines.push('## 执行步骤', '');
        steps.forEach(step => {
            lines.push(`[${formatAgentTime(step.created_at)}] ${step.title}${step.detail ? ` — ${step.detail}` : ''}`);
        });
        lines.push('');
    }
    if (messages.length) {
        lines.push('## 对话内容', '');
        messages.forEach(message => {
            lines.push(`[${formatAgentTime(message.created_at)}] ${message.role === 'user' ? '你' : 'Agent'}：`, message.content || '', '');
        });
    }
    const blob = new Blob([lines.join('\n')], {type: 'text/plain;charset=utf-8'});
    triggerDownload(URL.createObjectURL(blob), `agent-session-${String(session.session_id || 'export').slice(0, 8)}.txt`, true);
    showToast('会话记录已导出', 'success');
}

async function fetchAgentSession() {
    if (!agentSessionId) return;
    try {
        const response = await fetch(`/api/agent/sessions/${encodeURIComponent(agentSessionId)}`);
        if (!response.ok) {
            newAgentSession();
            return;
        }
        const result = await response.json();
        if (result.data?.expired) {
            newAgentSession();
            return;
        }
        if (result.success && result.data?.session) {
            renderAgentSession(result.data.session);
        }
    } catch (error) {
        debugLog('[Agent] session fetch failed:', error);
    }
}

function startAgentPolling() {
    if (agentPollTimer) return;
    agentPollTimer = setInterval(fetchAgentSession, 3000);
}

function stopAgentPolling() {
    if (agentPollTimer) {
        clearInterval(agentPollTimer);
        agentPollTimer = null;
    }
}

async function sendAgentMessage(execute = false, overrideMessage = '') {
    const input = $('agent-input');
    const message = (overrideMessage || input?.value || '').trim();
    if (!message && !execute) {
        showToast('请输入 Agent 指令', 'warning');
        return;
    }

    if (input && !overrideMessage) {
        if (message && message !== agentInputHistory[0]) {
            agentInputHistory.unshift(message);
            if (agentInputHistory.length > 50) agentInputHistory.length = 50;
            localStorage.setItem('gms_agent_input_history', JSON.stringify(agentInputHistory));
        }
        input.value = '';
        agentHistoryIndex = -1;
    }

    try {
        const container = $('agent-chat-messages');
        const typingEl = document.createElement('div');
        typingEl.id = 'agent-typing';
        typingEl.className = 'agent-typing';
        typingEl.textContent = '思考中...';
        if (container) { container.appendChild(typingEl); container.scrollTop = container.scrollHeight; }

        const response = await fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: agentSessionId || null,
                message: message || '确认执行',
                execute,
                workspace_context: getAgentWorkspaceContext()
            })
        });
        if (!response.ok) {
            let errorText = `HTTP ${response.status}`;
            try {
                const errorResult = await response.json();
                errorText = errorResult.error || errorResult.message || errorText;
            } catch (_) {
                // Keep the HTTP status when the server did not return JSON.
            }
            throw new Error(errorText);
        }
        const result = await response.json();

        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();

        if (!result.success) {
            showToast(result.error || 'Agent 请求失败', 'error');
            return;
        }
        renderAgentSession(result.data.session);
    } catch (error) {
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();
        showToast(`Agent 请求失败: ${error.message}`, 'error');
    }
}

async function sendAgentAction(action, paramsJson = '{}', label = '') {
    let params = {};
    try {
        params = JSON.parse(paramsJson || '{}');
    } catch (error) {
        showToast(`Agent 参数解析失败: ${error.message}`, 'error');
        return;
    }

    if (action === 'agent_elevate_retry') {
        if (agentElevationFlight) return;
        const sessionId = agentSessionId;
        const retryId = params.retry_id;
        if (!sessionId || retryId !== agentRetryId) return;
        const workspace = JSON.stringify(getAgentWorkspaceContext());
        agentElevationFlight = Promise.resolve().then(() => requestElevatedAccess('继续 Agent 资源查询'));
        try {
            const granted = await agentElevationFlight;
            if (!granted || agentSessionId !== sessionId || agentRetryId !== retryId
                || JSON.stringify(getAgentWorkspaceContext()) !== workspace) return;
            action = 'agent_retry';
            label = '授权完成，继续本轮规划';
        } catch (error) {
            showToast(`管理员授权失败: ${error.message}`, 'error');
            return;
        } finally {
            agentElevationFlight = null;
        }
    }
    const display = label || action;
    try {
        const container = $('agent-chat-messages');
        const typingEl = document.createElement('div');
        typingEl.id = 'agent-typing';
        typingEl.className = 'agent-typing';
        typingEl.textContent = '执行中...';
        if (container) { container.appendChild(typingEl); container.scrollTop = container.scrollHeight; }

        const response = await fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: agentSessionId || null,
                message: display,
                action,
                params,
                execute: false,
                workspace_context: getAgentWorkspaceContext()
            })
        });
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        if (!result.success) {
            showToast(result.error || 'Agent 操作失败', 'error');
            return;
        }
        renderAgentSession(result.data.session);
    } catch (error) {
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();
        showToast(`Agent 操作失败: ${error.message}`, 'error');
    }
}

function openAgentPageAction(page, paramsJson = '{}') {
    let params = {};
    try {
        params = JSON.parse(paramsJson || '{}');
    } catch (_) {
        params = {};
    }
    if (page === 'test-suites' && params.worker_id && params.suite_path) {
        const query = new URLSearchParams({worker_id: params.worker_id,
            suite_path: params.suite_path, path: params.path || ''});
        window.history.replaceState(null, '', '#test-suites?' + query.toString());
        switchPage(page, null);
        return;
    }
    if (page === 'redmine-agent') {
        const frame = document.getElementById('redmine-agent-frame');
        const query = new URLSearchParams();
        query.set('tab', params.tab || 'stats');
        if (params.name) query.set('name', params.name);
        if (frame) window.setLazyFrameSource?.(frame, '/redmine-agent?' + query.toString());
    }
    if (page === 'gerrit-dashboard') {
        const frame = document.getElementById('gerrit-dashboard-frame');
        if (frame) window.setLazyFrameSource?.(frame, '/gerrit-dashboard');
    }
    const contextPatch = {};
    if (params.worker_id) Object.assign(contextPatch, {worker_id: params.worker_id});
    if (params.devices) contextPatch.device_ids = params.devices;
    if (params.report_timestamp || params.timestamp) contextPatch.report_timestamp = params.report_timestamp || params.timestamp;
    if (params.issue_id) contextPatch.redmine_issue_id = String(params.issue_id);
    if (params.change_id) contextPatch.gerrit_change_id = String(params.change_id);
    if (Object.keys(contextPatch).length) window.GmsWorkspace?.update(contextPatch, {source: 'agent-navigation'});
    switchPage(page, null);
}

function confirmAgentPlan() {
    sendAgentMessage(true, '确认执行');
}

function newAgentSession() {
    clearAgentQuestions();
    agentSessionId = '';
    agentCurrentSession = null;
    agentDirectoryStateKey = '';
    localStorage.removeItem('gms_agent_session_id');
    stopAgentPolling();
    renderAgentSession({ status: 'idle', messages: [], steps: [] });
    refreshAgentSessionDirectory();
}

async function cancelAgentSession() {
    if (!agentSessionId) {
        newAgentSession();
        return;
    }

    try {
        const response = await fetch(`/api/agent/sessions/${encodeURIComponent(agentSessionId)}/cancel`, {
            method: 'POST'
        });
        const result = await response.json();
        if (result.success && result.data?.session) {
            renderAgentSession(result.data.session);
            refreshAgentSessionDirectory();
        } else {
            showToast(result.error || '取消失败', 'error');
        }
    } catch (error) {
        showToast(`取消失败: ${error.message}`, 'error');
    }
}

function openAgentReportAnalysis(timestamp) {
    window.GmsWorkspace?.update({report_timestamp: timestamp || '', origin_page: 'agent'}, {source: 'agent-report'});
    switchPage('report-analysis', null);
    if (typeof analyzeReport === 'function' && timestamp) {
        analyzeReport(timestamp);
    }
}

function openAgentApkAnalysis(taskId) {
    if (!taskId) return;
    switchPage('apk-analysis', null);
    if (typeof initApkAnalysisPage === 'function') {
        initApkAnalysisPage();
    }
    if (typeof stopApkPolling === 'function') {
        stopApkPolling();
    }
    window.apkCurrentTaskId = taskId;
    window.apkNotifiedTaskId = taskId;
    setApkUploadEmpty(false);
    if ($('apk-analysis-status')) $('apk-analysis-status').style.display = 'block';
    if ($('apk-analysis-result')) $('apk-analysis-result').style.display = 'block';
    if ($('apk-analysis-state')) $('apk-analysis-state').textContent = '正在加载 Agent 反编译任务...';
    pollApkStatus();
}

function initAgentPage() {
    if (!agentInitialized) {
        agentInitialized = true;
        const input = $('agent-input');
        if (input) {
            // Track current draft when user starts browsing history
            let agentDraftInput = '';

            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    sendAgentMessage(false);
                } else if (event.key === 'ArrowUp') {
                    // 历史命令非空时即可上翻浏览（未限制光标位置）。
                    if (agentInputHistory.length === 0) return;
                    // Save draft on first history navigation
                    if (agentHistoryIndex === -1) {
                        agentDraftInput = input.value;
                        agentHistoryIndex = 0;
                    } else if (agentHistoryIndex < agentInputHistory.length - 1) {
                        agentHistoryIndex++;
                    }
                    event.preventDefault();
                    input.value = agentInputHistory[agentHistoryIndex];
                    setTimeout(() => { input.selectionStart = input.selectionEnd = input.value.length; }, 0);
                } else if (event.key === 'ArrowDown') {
                    if (agentHistoryIndex === -1) return;
                    event.preventDefault();
                    if (agentHistoryIndex > 0) {
                        agentHistoryIndex--;
                        input.value = agentInputHistory[agentHistoryIndex];
                    } else {
                        agentHistoryIndex = -1;
                        input.value = agentDraftInput;
                    }
                    setTimeout(() => { input.selectionStart = input.selectionEnd = input.value.length; }, 0);
                }
            });

            input.addEventListener('input', () => {
                // Reset history browsing on manual input
                agentHistoryIndex = -1;
            });
        }
    }

    if (!agentModelStatusLoaded) loadAgentModelStatus(true, true);
    refreshAgentSessionDirectory();

    if (agentSessionId) {
        fetchAgentSession();
    } else {
        renderAgentSession({ status: 'idle', messages: [], steps: [] });
    }
}
