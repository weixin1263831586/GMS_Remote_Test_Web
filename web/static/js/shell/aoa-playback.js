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

// ---- 回放过程控制（暂停 / 继续 / 停止）----

function aoaPlaybackButtons() {
    return {
        pause: aoaEl('aoa-playback-pause'),
        resume: aoaEl('aoa-playback-resume'),
        cancel: aoaEl('aoa-playback-cancel'),
    };
}

function aoaPlaybackButtonState() {
    const buttons = aoaPlaybackButtons();
    if (!buttons.pause) return;
    const active = aoaPanelState.playbackActive && Boolean(aoaPanelState.playbackCommandId) && !aoaPanelState.playbackInputDone;
    const paused = aoaPanelState.playbackPaused;
    buttons.pause.hidden = !active || paused;
    buttons.resume.hidden = !active || !paused;
    buttons.cancel.hidden = !active;
    for (const button of Object.values(buttons)) if (button) button.disabled = Boolean(aoaPanelState.playbackPending) || !active;
    buttons.pause.textContent = aoaPanelState.playbackPending === 'pause' ? '正在暂停…' : '暂停';
    buttons.resume.textContent = aoaPanelState.playbackPending === 'resume' ? '正在继续…' : '继续';
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
    } catch (error) {
        if (id === aoaPanelState.playbackControlId) {
            aoaPanelState.playbackPending = '';
            aoaPlaybackButtonState();
            showToast(`回放控制请求失败：${error.message}`, 'warning');
        }
    }
}

function aoaPlaybackPause() { return aoaPlaybackControlRequest('pause'); }
function aoaPlaybackResume() { return aoaPlaybackControlRequest('resume'); }
async function aoaPlaybackCancel() {
    if (!aoaPanelState.playbackActive) return;
    if (await showConfirmDialog('停止回放', '将停止后续命令并清理 USB，已执行的输入不会回滚。继续？')) {
        return aoaPlaybackControlRequest('cancel');
    }
}
