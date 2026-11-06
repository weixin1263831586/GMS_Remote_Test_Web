// AOA 回放过程处理：进度事件、命令行渲染、暂停/继续/停止控制。
// 从 shell-aoa.js 拆出（frontend size ratchet：web/static/js/shell/shell-aoa.js
// 预算 51200 字节只减不增）；依赖 shell-aoa.js 的全局
// aoaPanelState / aoaEl / aoaApi / aoaSetStatus 与页面级 showToast、
// showConfirmDialog，均在事件回调运行时解析（defer 脚本加载顺序见 head.html）。

function aoaApplyPlaybackEvent(event) {
    const index = Number(event.index);
    const total = Number(event.total) || aoaPanelState.commands.length;
    const text = String(event.command || '');
    const marker = text.match(/^\[(paused|resumed|cancelled)\]$/);
    const phase = event.phase || marker?.[1] || 'running';
    if (phase === 'paused' || phase === 'resumed') {
        aoaPanelState.playbackPaused = phase === 'paused';
        aoaPanelState.playbackPending = '';
        if (phase === 'paused' && index >= 1) {
            aoaPanelState.playbackStates[index - 1] = 'paused';
            aoaPanelState.playbackIndex = index;
        }
        aoaUpdatePlaybackRows(index);
        aoaPlaybackButtonState();
        aoaSetStatus(phase === 'paused' ? `已暂停 · 下一条 ${index}/${total}` : `继续回放 · ${index}/${total}`, 'running');
        return;
    }
    if (index >= 1 && index <= aoaPanelState.commands.length) {
        for (let i = 0; i < index - 1; i++) aoaPanelState.playbackStates[i] = 'completed';
        aoaPanelState.playbackStates[index - 1] = phase === 'cancelled' ? 'cancelled' : phase;
        aoaPanelState.playbackIndex = index;
        aoaUpdatePlaybackRows(index);
    }
    if (phase === 'completed' && index === total) {
        aoaPanelState.playbackInputDone = true;
        aoaPanelState.playbackPending = '';
        aoaPlaybackButtonState();
        aoaSetStatus(`命令 ${total}/${total} 已执行完成，正在完成设备恢复检查…`, 'running');
    } else {
        const label = {failed: '执行失败', cancelled: '已停止', reconnecting: 'USB 重连中', checking: '检查页面', verifying: '验证结果', skipped: '状态已满足，跳过'};
        aoaSetStatus(`${label[phase] || '回放中'}：${index}/${total} ${text}`, phase === 'failed' ? 'error' : 'running');
    }
}

function aoaUpdatePlaybackRows(scrollIndex = 0) {
    const labels = {pending: '待执行', running: '执行中', reconnecting: 'USB 重连中', checking: '检查页面', verifying: '验证结果', skipped: '已跳过', completed: '已完成', paused: '已暂停', failed: '失败', cancelled: '已停止'};
    const rows = aoaEl('aoa-command-list')?.querySelectorAll('li') || [];
    rows.forEach((row, i) => {
        const state = aoaPanelState.playbackStates[i] || 'pending';
        row.classList.toggle('aoa-command-active', ['running', 'reconnecting', 'checking', 'verifying'].includes(state));
        row.classList.toggle('aoa-command-completed', state === 'completed' || state === 'skipped');
        row.classList.toggle('aoa-command-paused', state === 'paused');
        row.classList.toggle('aoa-command-failed', state === 'failed');
        if (state === 'running' || state === 'paused') row.setAttribute('aria-current', 'step');
        else row.removeAttribute('aria-current');
        const badge = row.querySelector('.aoa-command-state');
        if (badge) { badge.textContent = labels[state] || labels.pending; badge.dataset.state = state; }
    });
    const list = aoaEl('aoa-command-list');
    const row = rows[scrollIndex - 1];
    if (list && row) {
        const top = row.offsetTop - list.offsetTop;
        if (top < list.scrollTop || top + row.offsetHeight > list.scrollTop + list.clientHeight) {
            list.scrollTop = Math.max(0, top - list.clientHeight / 2);
        }
    }
}

function aoaHighlightCommandRow(index) {
    const rows = document.querySelectorAll('#aoa-command-list li');
    rows.forEach(row => row.classList.remove('aoa-command-active'));
    const target = rows[index - 1];
    if (!target) return;
    target.classList.add('aoa-command-active');
    target.scrollIntoView({block: 'nearest'});
}

// ---- 回放过程控制（暂停/继续合一按钮 + 停止）----

function aoaPlaybackButtons() {
    return {
        pause: aoaEl('aoa-playback-pause'),
    };
}

function aoaPlaybackButtonState() {
    const buttons = aoaPlaybackButtons();
    if (!buttons.pause) return;
    const active = aoaPanelState.playbackActive && Boolean(aoaPanelState.playbackCommandId) && !aoaPanelState.playbackInputDone;
    const paused = aoaPanelState.playbackPaused;
    // 暂停/继续复用同个按钮：hidden 只由"回放是否激活"决定，文案与
    // handler 随 paused 状态切换。
    buttons.pause.hidden = !active;
    // 暂停与停止的 disabled 都必须在这里统一管理：aoaSetBusy(true) 会把
    // [data-aoa-run] 全量禁用，回放启动（commandId 就绪）后由本函数解禁。
    buttons.pause.disabled = Boolean(aoaPanelState.playbackPending) || !active;
    const stop = aoaEl('aoa-record-stop');
    if (stop) {
        stop.disabled = Boolean(aoaPanelState.playbackPending) || !active;
        stop.classList.toggle('btn-danger', active);
        stop.classList.toggle('btn-xxs', true);
    }
    buttons.pause.dataset.mode = paused ? 'resume' : 'pause';
    buttons.pause.textContent = paused
        ? (aoaPanelState.playbackPending === 'resume' ? '正在继续…' : '继续')
        : (aoaPanelState.playbackPending === 'pause' ? '正在暂停…' : '暂停');
    buttons.pause.title = paused ? '继续回放' : '当前命令完成后暂停，保留设备租约；累计暂停最多 10 分钟';
}

function aoaPlaybackReset() {
    aoaPanelState.playbackControlId = '';
    aoaPanelState.playbackCommandId = '';
    aoaPanelState.playbackWorker = '';
    aoaPanelState.playbackPaused = false;
    aoaPanelState.playbackPending = '';
    aoaPanelState.playbackActive = false;
    aoaPlaybackButtonState();
}

async function aoaPlaybackControlRequest(action) {
    if (!aoaPanelState.playbackActive || !aoaPanelState.playbackCommandId || aoaPanelState.playbackPending || aoaPanelState.playbackInputDone) return;
    const id = aoaPanelState.playbackControlId;
    aoaPanelState.playbackPending = action;
    aoaPlaybackButtonState();
    try {
        await aoaApi(`/api/devices/aoa/playback/${encodeURIComponent(id)}/control`,
            {method: 'POST', body: {action, worker_id: aoaPanelState.playbackWorker}, timeoutMs: 10000});
        if (id !== aoaPanelState.playbackControlId || !aoaPanelState.playbackPending) return;
        aoaSetStatus(action === 'pause' ? '暂停请求已发送，当前命令结束后暂停…' : action === 'resume' ? '继续请求已发送…' : '正在停止回放…', 'running');
        // pending 只能由过程事件（paused/resumed/cancelled）解除；暂停必经
        // AOA 模式切换 → USB 重枚举，这个窗口里事件可能永久丢失（实机：
        // 暂停请求 200 后设备消失 1.5s，paused 事件未达，按钮卡在
        // "正在暂停…"不可再点）。15 秒无事件则主动对账命令状态并解除
        // 卡死，按钮恢复可用（再点可重发同一控制请求）。
        const settled = await aoaWaitPlaybackEventOrTimeout(id, action);
        if (id !== aoaPanelState.playbackControlId) return;
        if (!settled && aoaPanelState.playbackPending === action) {
            const status = await aoaReconcilePlaybackCommand(id);
            aoaPanelState.playbackPending = '';
            aoaPlaybackButtonState();
            showToast(status ? `暂停/继续状态已对账（当前：${status}）；如按钮未生效可再点一次`
                             : '控制事件等待超时，已恢复按钮；可再点一次重发请求', 'warning');
        }
    } catch (error) {
        if (id === aoaPanelState.playbackControlId) {
            aoaPanelState.playbackPending = '';
            aoaPlaybackButtonState();
            showToast(`回放控制请求失败：${error.message}`, 'warning');
        }
    }
}

// 有界等待过程事件：事件到达（pending 清零）返回 true；15 秒无事件返回
// false。只等待、不改状态。
async function aoaWaitPlaybackEventOrTimeout(controlId, action) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        if (aoaPanelState.playbackControlId !== controlId) return true;   // 回放已重置/结束
        if (aoaPanelState.playbackPending !== action) return true;        // 事件已解除 pending
    }
    return false;
}

// 主动对账：拉命令终态/运行态，仅供提示文案；事件通道仍是唯一状态源。
async function aoaReconcilePlaybackCommand(commandId) {
    try {
        const status = await aoaApi(
            `/api/cluster/commands/${encodeURIComponent(commandId)}`, {timeoutMs: 10000});
        return status?.command?.status || '';
    } catch (_) { return ''; }
}

function aoaPlaybackPause() { return aoaPlaybackControlRequest('pause'); }

// 暂停/继续共用按钮的统一入口：按按钮当前模式分发到对应请求。
function aoaPlaybackPauseResume() {
    const button = aoaEl('aoa-playback-pause');
    return aoaPlaybackControlRequest(button?.dataset.mode === 'resume' ? 'resume' : 'pause');
}
function aoaPlaybackResume() { return aoaPlaybackControlRequest('resume'); }
// 停止合一：回放激活 → 停止回放（带确认，复用 aoaPlaybackCancel 语义）；
// 否则 → 录制停止。按钮为单个 aoa-record-stop。
async function aoaPlaybackStop() {
    if (aoaPanelState.playbackActive) {
        return aoaPlaybackCancel();
    }
    return aoaRecordingStop();
}

async function aoaPlaybackCancel() {
    if (!aoaPanelState.playbackActive) return;
    if (await showConfirmDialog('停止回放', '将停止后续命令并清理 USB，已执行的输入不会回滚。继续？')) {
        return aoaPlaybackControlRequest('cancel');
    }
}
