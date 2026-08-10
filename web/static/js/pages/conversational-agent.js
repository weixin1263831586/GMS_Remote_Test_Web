// ==================== 对话Agent ====================
let agentSessionId = localStorage.getItem('gms_agent_session_id') || '';
let agentPollTimer = null;
let agentInitialized = false;
let agentInputHistory = JSON.parse(localStorage.getItem('gms_agent_input_history') || '[]');
let agentHistoryIndex = -1;  // -1 = not browsing history
const agentHandledAutoOpenMessageIds = new Set();

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
        cancelled: '已取消'
    };
    return labels[status] || status || '空闲';
}

function getCurrentPageName() {
    const active = document.querySelector('.page-content.active');
    return active?.id?.replace(/^page-/, '') || '';
}

function formatAgentTime(value) {
    if (!value) return '';
    const raw = String(value);
    const match = raw.match(/T(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : raw.replace('T', ' ');
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
        const item = line.replace(/^- /, '');
        const idx = item.indexOf(':');
        if (line.startsWith('- ') && idx > 0) {
            fields.push([item.slice(0, idx).trim(), item.slice(idx + 1).trim()]);
        } else if (/输入|当前没有|不能执行/.test(line)) {
            notes.push(line);
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

        // Plan confirmation buttons
        if (plan && session.status === 'planning') {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" onclick="confirmAgentPlan()">执行计划</button>
                    <button class="btn-xs" onclick="sendAgentMessage(false, '重新规划')">重新规划</button>
                </div>
            `;
        }

        // Report analysis buttons
        if (reportTimestamp) {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" onclick="openAgentReportAnalysis('${escapeJsAttr(reportTimestamp)}')">打开报告分析</button>
                    <button class="btn-xs" onclick="switchPage('reports', null)">报告管理</button>
                    ${apkTaskId ? `<button class="btn-xs" onclick="openAgentApkAnalysis('${escapeJsAttr(apkTaskId)}')">打开APK分析</button>` : ''}
                </div>
            `;
        }

        // Quick actions from response generator
        if (quickActions.length > 0) {
            const actionBtns = quickActions.map(a => {
                if (a.page) {
                    return `<button class="btn-xs" onclick="openAgentPageAction('${escapeJsAttr(a.page)}', '${escapeJsAttr(JSON.stringify(a.params || {}))}')">${escapeHtml(a.label)}</button>`;
                } else if (a.action) {
                    return `<button class="btn-xs" onclick="sendAgentAction('${escapeJsAttr(a.action)}', '${escapeJsAttr(JSON.stringify(a.params || {}))}', '${escapeJsAttr(a.label || a.action)}')">${escapeHtml(a.label)}</button>`;
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
                    <button class="btn-xs" onclick="switchPage('${escapeJsAttr(targetPage)}', null)">打开页面</button>
                </div>
            `;
        }

        const escapedContent = escapeHtml(message.content || '').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        const contentHtml = isPlanLike ? renderAgentPlanContent(message.content || '') : escapedContent;
        const toneClass = tone ? ` ${tone}` : '';
        const bodyClass = isPlanLike ? 'agent-message-body plan-body' : 'agent-message-body';

        return `
            <div class="agent-message-row ${isUser ? 'user' : 'assistant'}">
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

    const colorByStatus = {
        done: 'var(--success-color)',
        running: 'var(--primary-color)',
        warning: 'var(--warning-color)',
        error: 'var(--danger-color)'
    };
    const iconByStatus = {
        done: '✓',
        running: '…',
        warning: '!',
        error: '×'
    };

    stepsEl.innerHTML = steps.map(step => {
        const color = colorByStatus[step.status] || 'var(--text-secondary)';
        const icon = iconByStatus[step.status] || '•';
        return `
            <div style="border: 1px solid var(--border-color); border-radius: 6px; padding: 9px; background: var(--darker-bg);">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 5px;">
                    <span style="width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; background: ${color}; color: #fff; font-size: 11px; flex-shrink: 0;">${icon}</span>
                    <span style="font-size: 13px; font-weight: 600; color: var(--text-primary);">${escapeHtml(step.title || '')}</span>
                </div>
                <div style="font-size: 12px; color: var(--text-secondary); line-height: 1.45; overflow-wrap: anywhere;">${escapeHtml(step.detail || '')}</div>
            </div>
        `;
    }).join('');
}

function renderAgentSession(session) {
    if (!session) return;
    agentSessionId = session.session_id || agentSessionId;
    if (agentSessionId) {
        localStorage.setItem('gms_agent_session_id', agentSessionId);
    }
    renderAgentMessages(session);
    renderAgentSteps(session);
    applyAgentSessionWorkspace(session);

    if (['running', 'monitoring'].includes(session.status)) {
        startAgentPolling();
    } else if (agentPollTimer) {
        stopAgentPolling();
    }
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
        // Save to history before clearing
        if (message && message !== agentInputHistory[0]) {
            agentInputHistory.unshift(message);
            if (agentInputHistory.length > 50) agentInputHistory.length = 50;
            localStorage.setItem('gms_agent_input_history', JSON.stringify(agentInputHistory));
        }
        input.value = '';
        agentHistoryIndex = -1;
        // Reset height
        input.style.height = 'auto';
        input.style.height = '100px';
    }

    try {
        // Show typing indicator
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

        // Remove typing indicator
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
    if (page === 'redmine-agent') {
        const frame = document.getElementById('redmine-agent-frame');
        const query = new URLSearchParams();
        query.set('tab', params.tab || 'stats');
        if (params.name) query.set('name', params.name);
        if (frame) frame.src = '/redmine-agent?' + query.toString();
    }
    if (page === 'gerrit-dashboard') {
        const frame = document.getElementById('gerrit-dashboard-frame');
        if (frame) frame.src = '/gerrit-dashboard';
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
    agentSessionId = '';
    localStorage.removeItem('gms_agent_session_id');
    stopAgentPolling();
    renderAgentSession({ status: 'idle', messages: [], steps: [] });
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
                    // 光标在开头或输入为空时才浏览历史命令。
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
                    // Move cursor to end
                    setTimeout(() => { input.selectionStart = input.selectionEnd = input.value.length; }, 0);
                } else if (event.key === 'ArrowDown') {
                    if (agentHistoryIndex === -1) return;
                    event.preventDefault();
                    if (agentHistoryIndex > 0) {
                        agentHistoryIndex--;
                        input.value = agentInputHistory[agentHistoryIndex];
                    } else {
                        // Restore draft
                        agentHistoryIndex = -1;
                        input.value = agentDraftInput;
                    }
                    setTimeout(() => { input.selectionStart = input.selectionEnd = input.value.length; }, 0);
                }
            });

            // Auto-resize textarea based on content
            input.addEventListener('input', () => {
                // Reset history browsing on manual input
                agentHistoryIndex = -1;
                input.style.height = 'auto';
                input.style.height = Math.max(100, Math.min(input.scrollHeight, 300)) + 'px';
            });
        }
    }

    if (agentSessionId) {
        fetchAgentSession();
    } else {
        renderAgentSession({ status: 'idle', messages: [], steps: [] });
    }
}


