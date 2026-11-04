// ==================== Web AOA 三列工作台（设备管理页） ====================
// 左：ADB 截图投屏与 UI 输入；中：录制/Profile/目标回放；右：AOA 命令轨道。
// 真正的输入和回放都经 Controller 的设备 claim/fencing → Worker 边界执行，
// 本文件不修改 Test Workspace 的 worker/device 选择。
'use strict';

const AOA_TOUCH_CALIBRATION_VERSION = 3;

const aoaPanelState = {
    profiles: [], devices: [], commands: [], loaded: false, loading: false,
    running: false, recording: false, editingProfileId: '', screenRefreshId: 0,
    calibration: null, pointer: null, autoConnectDone: false,
    profileMetadata: {},
    hidSettingsProfileId: '',
    playbackControlId: '', playbackPaused: false, playbackActive: false,
    playbackCommandId: '', playbackWorker: '', playbackPending: '', playbackInputDone: false,
    playbackStates: [], playbackIndex: 0, commandDebug: null, commandEdit: null, commandsProfileId: '',
};

function aoaEl(id) { return document.getElementById(id); }

function aoaHidMetadata() {
    return {...aoaPanelState.profileMetadata,
        hid_touch_mode: aoaEl('aoa-hid-touch-mode')?.value || 'pointer',
        hid_session_mode: aoaEl('aoa-hid-session-mode')?.value || 'continuous',
        hid_settle_seconds: Number(aoaEl('aoa-hid-settle')?.value || 2)};
}

function aoaLoadHidMetadata(metadata = {}) {
    const context = metadata.calibration?.context;
    const inferred = context?.version === 2 ? context.hid_touch_mode || 'direct' : 'pointer';
    const mode = metadata.hid_touch_mode || metadata.calibration?.hid_touch_mode || inferred;
    const settle = metadata.hid_settle_seconds ?? 2;
    const sessionMode = metadata.hid_session_mode || 'continuous';
    if (!['pointer', 'direct'].includes(mode) || typeof settle !== 'number'
            || !Number.isFinite(settle) || settle < 0 || settle > 10
            || !['continuous', 'per_command'].includes(sessionMode)) {
        throw new Error('AOA 输入模式或稳定等待无效，请检查配置');
    }
    aoaPanelState.profileMetadata = {...metadata};
    aoaPanelState.hidSettingsProfileId = '';
    aoaEl('aoa-hid-touch-mode').value = mode;
    aoaEl('aoa-hid-settle').value = String(settle);
    aoaEl('aoa-hid-session-mode').value = sessionMode;
    if (aoaPanelState.calibration?.context?.hid_touch_mode !== aoaEl('aoa-hid-touch-mode').value) {
        aoaPanelState.calibration = null; aoaRenderCalibration();
    }
}

function aoaHidModeChanged() {
    aoaHidOptionEdited('aoa-hid-touch-mode');
    aoaPanelState.calibration = null;
    aoaPanelState.profileMetadata = aoaHidMetadata();
    delete aoaPanelState.profileMetadata.calibration;
    aoaRenderCalibration();
    aoaSetStatus('输入模式已改变；请在该模式下重新标定和验证坐标');
}

function aoaHidOptionEdited(field) {
    const profile = aoaSelectedProfile();
    const value = aoaEl(field).value;
    if (profile && aoaPanelState.commandsProfileId !== profile.id
            && aoaPanelState.hidSettingsProfileId !== profile.id) {
        aoaLoadHidMetadata(profile.metadata || {});
        aoaEl(field).value = value;
    }
    aoaPanelState.hidSettingsProfileId = profile?.id || '';
}

function aoaHidSettleChanged() { aoaHidOptionEdited('aoa-hid-settle'); }

function aoaSessionModeChanged() {
    aoaHidOptionEdited('aoa-hid-session-mode');
    aoaSetStatus(aoaEl('aoa-hid-session-mode').value === 'per_command'
        ? '按单条方式回放：每个输入动作结束后恢复 USB，速度较慢'
        : '连续回放：整段保持 USB/HID 连接');
}

function aoaMessage(error) {
    const detail = error?.detail || error?.error || error;
    if (typeof detail === 'string') return detail;
    const text = detail?.message || detail?.detail || '';
    if (text) return String(text);
    // 兜底返回空串：调用方回落到 "HTTP <status>"，避免渲染成无意义的 "{}"。
    return '';
}

async function aoaApi(path, options = {}) {
    const {timeoutMs = 0, ...fetchOptions} = options;
    const request = {...fetchOptions, headers: {...(fetchOptions.headers || {})}};
    if (request.body && typeof request.body !== 'string') {
        request.headers['Content-Type'] = 'application/json';
        request.body = JSON.stringify(request.body);
    }
    const controller = timeoutMs ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    if (controller) request.signal = controller.signal;
    try {
        const response = await fetch(path, request);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.success === false) {
            const error = new Error(aoaMessage(payload) || `HTTP ${response.status}`);
            error.status = response.status;
            throw error;
        }
        return payload.data || payload;
    } finally {
        if (timer !== null) clearTimeout(timer);
    }
}

async function aoaAwaitCommand(payload, {onProgress = aoaApplyPlaybackEvent, trackPlayback = true, debugSequence = 0} = {}) {
    if (!payload?.accepted) return payload;
    const commandId = payload.command_id;
    if (!commandId) throw new Error('Worker 已接受操作但未返回 command_id');
    let eventCursor = -1;
    let debugCompleted = false;
    const deadline = Date.now() + (Number(payload.operation_timeout_seconds) || 960) * 1000;
    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        // 增量拉取 Worker 上报的过程事件：progress 事件高亮对应命令行
        // 并自动滚动；网络抖动仅跳过本轮，不影响执行与终态轮询。
        try {
            const events = await aoaApi(
                `/api/cluster/commands/${encodeURIComponent(commandId)}/events?after=${eventCursor}`, {timeoutMs: 15000});
            (events.events || []).forEach(event => {
                if (event.sequence > eventCursor) eventCursor = event.sequence;
                if (event.event_type !== 'progress') return;
                if (debugSequence && Number(event.payload?.debug_sequence) !== debugSequence) return;
                if (debugSequence && event.payload?.phase === 'completed') debugCompleted = true;
                onProgress(event.payload || {});
            });
        } catch (error) { /* 事件通道暂不可用：继续等终态 */ }
        let status;
        try {
            status = await aoaApi(`/api/cluster/commands/${encodeURIComponent(commandId)}`, {timeoutMs: 15000});
        } catch (error) {
            if (!error.status || [408, 429, 502, 503, 504].includes(error.status)) {
                aoaSetStatus(`连接中断，继续查询原命令 ${commandId}…`, 'running');
                continue;
            }
            throw new Error(`${error.message}（命令 ${commandId}；请核对原操作状态）`);
        }
        const command = status.command || {};
        if (debugSequence && debugCompleted && ['running', 'completed'].includes(command.status)) {
            return {success: true, commands_executed: 1, hid_session_kept: command.status === 'running'};
        }
        if (command.status === 'completed') {
            if (debugSequence) throw new Error('调试连接已结束，此条命令未收到执行成功回执；请核对设备状态');
            const result = command.result || {};
            const completed = Number(result.commands_executed);
            if (trackPlayback && Number.isFinite(completed)) {
                for (let i = 0; i < completed; i++) aoaPanelState.playbackStates[i] = 'completed';
                aoaUpdatePlaybackRows(0);
            } else if (trackPlayback) aoaHighlightCommandRow(0);
            return result;
        }
        if (['failed', 'cancelled'].includes(command.status)) {
            const index = aoaPanelState.playbackIndex;
            if (trackPlayback && aoaPanelState.playbackActive && index > 0 && aoaPanelState.playbackStates[index - 1] !== 'completed') {
                aoaPanelState.playbackStates[index - 1] = command.status;
                aoaUpdatePlaybackRows(0);
            } else if (trackPlayback) aoaHighlightCommandRow(0);
            throw new Error(command.error || 'Worker 设备操作失败');
        }
    }
    throw new Error(`等待操作结果超时，请查询命令 ${commandId}；不要重新回放`);
}


function aoaSetStatus(text, type = '') {
    const node = aoaEl('aoa-run-status');
    if (!node) return;
    node.textContent = text || '待机';
    node.title = node.textContent;
    node.dataset.type = type;
}

function aoaSetBusy(running, text = '') {
    running = running || aoaDebugState.ending;
    aoaPanelState.running = running;
    document.querySelectorAll('#aoa-panel [data-aoa-run], #aoa-panel [data-aoa-screen-action], #aoa-panel select, #aoa-panel [data-aoa-edit], #aoa-command-list button, #aoa-command-list input, #aoa-command-input, #aoa-record-start, #aoa-record-stop, #aoa-profile-name, #aoa-replay-wait')
        .forEach(node => { node.disabled = running; });
    if (text) aoaSetStatus(text, running ? 'running' : '');
    if (aoaEl('aoa-record-start')) aoaEl('aoa-record-start').disabled = running || aoaPanelState.recording;
    if (aoaEl('aoa-record-stop')) aoaEl('aoa-record-stop').disabled = running || !aoaPanelState.recording;
    aoaPlaybackButtonState();
    aoaDebugButtonState();
}

function aoaDeviceWorker(device) {
    return String(device?.worker_id || device?.source_host || '');
}

function aoaDeviceSerial(device) {
    return String(device?.serial_no || device?.serial || '').split(':').pop();
}

function aoaDeviceLabel(device) {
    const suffix = [device?.model, device?.android_version || device?.release]
        .filter(Boolean).join(' · ');
    return `${aoaDeviceSerial(device)}${suffix ? ` · ${suffix}` : ''}`;
}

function aoaReplaceOptions(select, entries, emptyLabel, keepMissing = false) {
    if (!select) return;
    const previous = select.value;
    if (keepMissing && previous && !entries.some(entry => entry.value === previous)) {
        const label = select.selectedOptions[0]?.dataset.label || previous;
        entries = [{value: previous, label, missing: true}, ...entries];
    }
    select.replaceChildren();
    if (!entries.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = emptyLabel;
        select.append(option);
        return;
    }
    entries.forEach(entry => {
        const option = document.createElement('option');
        option.value = entry.value;
        option.textContent = entry.label + (entry.missing ? '（暂未枚举）' : '');
        option.dataset.label = entry.label;
        select.append(option);
    });
    if (entries.some(entry => entry.value === previous)) select.value = previous;
}

function aoaUsableDevices(workerId, recorder = false) {
    return aoaPanelState.devices.filter(device => {
        if (aoaDeviceWorker(device) !== workerId || device.transport === 'adb_proxy') return false;
        if (!recorder) return true;
        const state = String(
            device.cluster_state || device.state || device.status || ''
        ).toLowerCase();
        return !['offline', 'unknown', 'fastboot', 'usb_only'].includes(state);
    });
}

function aoaFillDeviceSelect(selectId, workerId, recorder = false, keepMissing = false) {
    const devices = aoaUsableDevices(workerId, recorder);
    aoaReplaceOptions(aoaEl(selectId), devices.map(device => ({
        value: aoaDeviceSerial(device), label: aoaDeviceLabel(device),
    })), recorder ? '（无 ADB 可用录制设备）' : '（该 Worker 无本地 USB 设备）', keepMissing);
}

function aoaSyncDeviceOptions() {
    aoaFillDeviceSelect('aoa-record-device', aoaEl('aoa-record-worker')?.value || '', true);
    if (!aoaDebugSyncTarget()) aoaFillDeviceSelect('aoa-replay-device', aoaEl('aoa-replay-worker')?.value || '', false, true);
}

// 设备管理清单刷新后的只读同步入口。
function aoaSyncDeviceSources(devices) {
    if (!aoaEl('aoa-panel')) return;
    aoaPanelState.devices = Array.isArray(devices) ? devices : [];
    const workers = [...new Set(aoaPanelState.devices
        .filter(device => device.transport !== 'adb_proxy')
        .map(aoaDeviceWorker).filter(Boolean))];
    const entries = workers.map(worker => ({value: worker, label: worker}));
    aoaReplaceOptions(aoaEl('aoa-record-worker'), entries, '（无可用 Worker）');
    aoaReplaceOptions(aoaEl('aoa-replay-worker'), entries, '（无可用 Worker）', true);
    aoaSyncDeviceOptions();

}

function aoaRecorderWorkerChanged() {
    aoaFillDeviceSelect('aoa-record-device', aoaEl('aoa-record-worker')?.value || '', true);
    aoaRecorderDeviceChanged();
}

function aoaReplayWorkerChanged() {
    if (aoaDebugSyncTarget()) return;
    aoaFillDeviceSelect('aoa-replay-device', aoaEl('aoa-replay-worker')?.value || '', false);
}

function aoaRecorderDeviceChanged() {
    aoaPanelState.calibration = null;
    const key = `gms-aoa-calibration:${aoaEl('aoa-record-worker')?.value || ''}:${aoaEl('aoa-record-device')?.value || ''}`;
    try {
        const saved = JSON.parse(localStorage.getItem(key) || 'null');
        if (saved?.context?.version === AOA_TOUCH_CALIBRATION_VERSION
                && (saved.context.hid_touch_mode || 'direct') === aoaHidMetadata().hid_touch_mode) {
            aoaPanelState.calibration = saved;
        }
    } catch (_) { /* storage unavailable */ }
    aoaRenderCalibration();
    // 切换设备：实时投屏会话属于旧设备，必须停止；随后自动续上新画面
    // （之前在实时投屏 → 自动对新设备重启流，失败内部回退截图轮询；
    //  截图模式 → 自动拉一帧），不再要求手动点“刷新”。
    if (window.aoaLiveScreen?.isActive()) {
        window.aoaLiveScreen.stop();
        aoaEl('aoa-toggle-live')?.setAttribute('data-live', 'off');
    }
    const image = aoaEl('aoa-screen-image');
    if (image) { image.hidden = true; image.removeAttribute('src'); }
    const canvas = aoaEl('aoa-screen-canvas');
    if (canvas) { canvas.hidden = true; }
    const empty = aoaEl('aoa-screen-empty');
    if (empty) { empty.hidden = true; }
    const target = aoaLiveTarget();
    if (!target) {
        if (empty) empty.hidden = false;
        const status = aoaEl('aoa-screen-status');
        if (status) status.textContent = '选择录制设备后将自动连接实时投屏';
        aoaRenderCalibration();
        return;
    }
    // 一律自动优先实时流（scrcpy）：成功即画面实时更新；设备不支持/流
    // 失败时 aoaLiveScreen.start 内部自动回退截图轮询。「▶」按钮仅作为
    // 手动停止/重开投屏的开关。
    const status = aoaEl('aoa-screen-status');
    if (status) status.textContent = `正在连接 ${target.device} 实时投屏…`;
    window.aoaLiveScreen.start(target.device, target.workerId).then(() => {
        aoaEl('aoa-toggle-live')?.setAttribute(
            'data-live', window.aoaLiveScreen.isActive() ? 'on' : 'off');
    });
    aoaRenderCalibration();
}

async function aoaLoadPanel() {
    if (aoaPanelState.loading) return;
    aoaPanelState.loading = true;
    try {
        const data = await aoaApi('/api/devices/aoa/profiles');
        aoaPanelState.profiles = data.profiles || [];
        aoaPanelState.loaded = true;
        aoaRenderProfiles();
    } catch (error) {
        aoaSetStatus(`配置加载失败：${error.message}`, 'error');
        if (typeof showToast === 'function') showToast(`AOA 配置加载失败：${error.message}`, 'warning');
    } finally {
        aoaPanelState.loading = false;
    }
}

function aoaRenderProfiles() {
    const select = aoaEl('aoa-replay-profile');
    const previous = select?.value;
    aoaReplaceOptions(select, aoaPanelState.profiles.map(profile => ({
        value: profile.id,
        label: `${profile.name}（${(profile.commands || []).length} 条）`,
    })), '（暂无配置）');
    if (previous && aoaPanelState.profiles.some(profile => profile.id === previous)) {
        select.value = previous;
    }
    aoaProfileSelectionChanged();
}

function aoaSelectedProfile() {
    const id = aoaEl('aoa-replay-profile')?.value || '';
    return aoaPanelState.profiles.find(profile => profile.id === id) || null;
}

function aoaProfileSelectionChanged() {
    const profile = aoaSelectedProfile();
    const node = aoaEl('aoa-profile-summary');
    if (!node) return;
    node.textContent = profile
        ? `${profile.name} · ${(profile.commands || []).length} 条命令 · ADB 等待 ${profile.wait_adb_seconds || 0}s · 更新 ${(profile.updated_at || profile.created_at || '').replace('T', ' ').slice(0, 16)}`
        : '暂无配置';
}

function aoaLoadSelectedProfile() {
    if (aoaPanelState.playbackActive) return;
    const profile = aoaSelectedProfile();
    if (!profile) { showToast('请先选择一个 AOA 配置', 'warning'); return; }
    const commands = (profile.commands || []).map(command => String(command).trim()).filter(Boolean);
    aoaPlaybackReset();
    aoaPanelState.commandDebug = null;
    aoaPanelState.commandEdit = null;
    aoaPanelState.playbackStates = [];
    aoaPanelState.commands = [...commands];
    aoaPanelState.editingProfileId = profile.id;
    aoaPanelState.commandsProfileId = profile.id;
    aoaEl('aoa-profile-name').value = profile.name;
    aoaLoadHidMetadata(profile.metadata || {});
    aoaEl('aoa-replay-wait').value = profile.wait_adb_seconds || 60;
    aoaRenderCommands();
    aoaSetStatus(`已载入：${profile.name}`);
}

function aoaRenderCommands() {
    const editing = aoaPanelState.commandEdit;
    if (editing && (aoaPanelState.playbackActive || aoaPanelState.commands[editing.index] !== editing.original)) {
        aoaPanelState.commandEdit = null;
    }
    const list = aoaEl('aoa-command-list');
    const count = aoaEl('aoa-command-count');
    if (count) count.textContent = String(aoaPanelState.commands.length);
    if (!list) return;
    list.replaceChildren();
    if (!aoaPanelState.playbackActive) aoaPanelState.playbackStates = [];
    if (!aoaPanelState.commands.length) {
        const empty = document.createElement('li');
        empty.className = 'aoa-command-empty';
        empty.textContent = '开始录制后，在左侧操作设备 UI';
        list.append(empty);
        return;
    }
    aoaPanelState.commands.forEach((command, i) => {
        const row = document.createElement('li');
        const content = document.createElement('div');
        content.className = 'aoa-command-row';
        const index = document.createElement('span');
        index.className = 'aoa-command-index';
        index.textContent = String(i + 1);
        const code = document.createElement('code');
        code.textContent = command;
        const edit = document.createElement('button');
        edit.className = 'btn-xxs aoa-command-edit'; edit.type = 'button'; edit.textContent = '编辑';
        edit.disabled = aoaPanelState.running || aoaPanelState.playbackActive;
        edit.setAttribute('aria-label', `编辑第 ${i + 1} 条命令`);
        edit.addEventListener('click', () => aoaPanelState.commandEdit?.index === i
            ? aoaCommandEditFinish() : aoaCommandEditStart(i));
        const execute = document.createElement('button');
        execute.className = 'btn-xxs aoa-command-execute'; execute.type = 'button'; execute.textContent = '执行';
        execute.disabled = aoaPanelState.running || aoaPanelState.playbackActive;
        execute.title = '在选中的目标设备上仅执行这一条 AOA 命令';
        execute.setAttribute('aria-label', `执行第 ${i + 1} 条命令`);
        execute.addEventListener('click', () => aoaCommandExecute(i));
        const remove = document.createElement('button');
        remove.className = 'btn-xxs'; remove.type = 'button'; remove.textContent = '删除';
        remove.disabled = aoaPanelState.running;
        remove.addEventListener('click', () => {
            if (aoaPanelState.running) return;
            if (!aoaCommandEditFinish()) return;
            aoaPanelState.commands.splice(i, 1);
            aoaPanelState.editingProfileId = '';
            aoaRenderCommands();
        });
        const state = document.createElement('span'); state.className = 'aoa-command-state';
        state.hidden = !aoaPanelState.playbackActive;
        const actions = document.createElement('div'); actions.className = 'aoa-command-actions';
        actions.append(edit, execute, remove);
        let commandNode = code;
        if (aoaPanelState.commandEdit?.index === i) {
            const input = document.createElement('input');
            input.className = 'aoa-command-editor'; input.type = 'text';
            input.value = aoaPanelState.commandEdit.draft;
            input.setAttribute('aria-label', `第 ${i + 1} 条命令内容`);
            input.disabled = aoaPanelState.running || aoaPanelState.playbackActive;
            input.addEventListener('input', () => { aoaPanelState.commandEdit.draft = input.value; });
            input.addEventListener('keydown', event => {
                if (event.isComposing) return;
                if (event.key === 'Enter' || event.key === 'Escape') {
                    event.preventDefault(); aoaCommandEditFinish(event.key === 'Enter');
                }
            });
            edit.textContent = '保存'; edit.setAttribute('aria-label', `保存第 ${i + 1} 条命令`);
            const cancel = document.createElement('button');
            cancel.className = 'btn-xxs'; cancel.type = 'button'; cancel.textContent = '取消';
            cancel.disabled = input.disabled;
            cancel.addEventListener('click', () => aoaCommandEditFinish(false));
            edit.after(cancel);
            commandNode = input;
        }
        content.append(index, commandNode, state, actions); row.append(content); list.append(row);
    });
    list.scrollTop = aoaPanelState.playbackActive ? 0 : list.scrollHeight;
    if (aoaPanelState.playbackActive) aoaUpdatePlaybackRows(aoaPanelState.playbackIndex);
    else if (aoaPanelState.commandDebug) {
        const debug = aoaPanelState.commandDebug;
        if (aoaPanelState.commands[debug.index] === debug.command) aoaUpdateCommandDebug(debug.state);
        else aoaPanelState.commandDebug = null;
    }
}

function aoaCommandEditStart(index) {
    if (aoaPanelState.running || aoaPanelState.playbackActive || !aoaCommandEditFinish()) return;
    const command = aoaPanelState.commands[index];
    if (typeof command !== 'string') return;
    const list = aoaEl('aoa-command-list'); const scroll = list.scrollTop;
    aoaPanelState.commandEdit = {index, original: command, draft: command};
    aoaRenderCommands(); list.scrollTop = scroll;
    const input = list.querySelector('.aoa-command-editor'); input.focus(); input.select();
}

function aoaCommandEditFinish(save = true) {
    const edit = aoaPanelState.commandEdit;
    if (!edit) return true;
    if (aoaPanelState.running || aoaPanelState.playbackActive) return false;
    const command = edit.draft.trim();
    if (save && !command) {
        aoaSetStatus('命令不能为空，请修改或取消编辑', 'warning');
        aoaEl('aoa-command-list')?.querySelector('.aoa-command-editor')?.focus(); return false;
    }
    if (save && command !== aoaPanelState.commands[edit.index]) {
        aoaPanelState.commands[edit.index] = command;
        aoaPanelState.editingProfileId = '';
        aoaPanelState.commandDebug = null;
        if (aoaEl('aoa-playback-progress')) aoaEl('aoa-playback-progress').hidden = true;
        aoaSetStatus(`第 ${edit.index + 1} 条已修改，保存配置后可持久保留`);
    }
    const list = aoaEl('aoa-command-list'); const scroll = list.scrollTop;
    aoaPanelState.commandEdit = null;
    aoaRenderCommands(); list.scrollTop = scroll;
    return true;
}

function aoaUpdateCommandDebug(state) {
    const debug = aoaPanelState.commandDebug;
    if (!debug || aoaPanelState.commands[debug.index] !== debug.command) return;
    debug.state = state;
    const row = aoaEl('aoa-command-list')?.children[debug.index];
    if (!row) return;
    row.classList.toggle('aoa-command-active', state === 'running');
    row.classList.toggle('aoa-command-completed', state === 'completed');
    row.classList.toggle('aoa-command-failed', state === 'failed');
    const badge = row.querySelector('.aoa-command-state');
    if (badge) {
        badge.hidden = false; badge.dataset.state = state;
        badge.textContent = state === 'running' ? '执行中' : state === 'completed' ? '已执行' : state === 'cancelled' ? '已停止' : '执行失败';
    }
    const progress = aoaEl('aoa-playback-progress');
    if (progress) {
        progress.hidden = false;
        progress.textContent = `单条调试 · 第 ${debug.index + 1} 条 · ${debug.device} · ${badge?.textContent || ''}`;
        progress.title = progress.textContent;
    }
}


function aoaAppendCommand(command, addPace = true) {
    if (aoaPanelState.playbackActive) return;
    if (!command) return;
    const additions = [command];
    if (addPace && !command.startsWith('sleep ')) additions.push('sleep 800');
    if (aoaPanelState.commands.length + additions.length > 256) {
        aoaRecordingStop();
        throw new Error('AOA 命令已达到 256 条上限，请保存当前配置');
    }
    aoaPanelState.commands.push(...additions);
    aoaPanelState.editingProfileId = '';
    aoaRenderCommands();
}

async function aoaRecordingStart() {
    if (aoaPanelState.playbackActive) return;
    if (aoaPanelState.commands.length) {
        const ok = await showConfirmDialog('开始新录制', '当前命令会被清空，是否继续？');
        if (!ok || aoaPanelState.running) return;
    }
    if (!aoaEl('aoa-record-worker')?.value || !aoaEl('aoa-record-device')?.value) {
        showToast('请选择 ADB 可用的录制 Worker 和设备', 'warning'); return;
    }
    aoaPanelState.commands = []; aoaPanelState.editingProfileId = '';
    aoaPanelState.commandEdit = null;
    aoaPanelState.commandsProfileId = '';
    aoaPanelState.recording = true;
    aoaEl('aoa-record-start').disabled = true;
    aoaEl('aoa-record-stop').disabled = false;
    aoaEl('aoa-record-state').textContent = '录制中：左侧成功操作会写入命令';
    aoaSetStatus('录制中', 'running'); aoaRenderCommands();
    if (aoaEl('aoa-screen-image')?.hidden) await aoaRefreshScreen();
}

function aoaRecordingStop() {
    aoaPanelState.recording = false;
    if (aoaEl('aoa-record-start')) aoaEl('aoa-record-start').disabled = false;
    if (aoaEl('aoa-record-stop')) aoaEl('aoa-record-stop').disabled = true;
    if (aoaEl('aoa-record-state')) {
        aoaEl('aoa-record-state').textContent = `已停止 · ${aoaPanelState.commands.length} 条命令`;
    }
    aoaSetStatus('录制已停止');
}

function aoaCommandUndo() {
    if (aoaPanelState.playbackActive) return;
    if (!aoaCommandEditFinish()) return;
    if (!aoaPanelState.commands.length) return;
    if (aoaPanelState.commands.at(-1)?.startsWith('sleep ')) aoaPanelState.commands.pop();
    if (aoaPanelState.commands.length) aoaPanelState.commands.pop();
    aoaRenderCommands();
}

async function aoaCommandsClear() {
    if (aoaPanelState.playbackActive) return;
    if (!aoaPanelState.commands.length) return;
    const ok = await showConfirmDialog('清空 AOA 命令', '确定清空当前命令轨道？');
    if (!ok) return;
    aoaPanelState.commands = []; aoaPanelState.editingProfileId = '';
    aoaPanelState.commandEdit = null;
    aoaRenderCommands();
}

function aoaCommandAdd() {
    if (aoaPanelState.playbackActive) return;
    const input = aoaEl('aoa-command-input');
    const command = input?.value.trim();
    if (!command) return;
    try { aoaAppendCommand(command, false); input.value = ''; }
    catch (error) { showToast(error.message, 'warning'); }
}

function aoaCommandsExport() {
    if (!aoaCommandEditFinish()) return;
    if (!aoaPanelState.commands.length) { showToast('当前没有可导出的命令', 'warning'); return; }
    const payload = {
        version: 1,
        name: aoaEl('aoa-profile-name')?.value.trim() || 'AOA Profile',
        wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || 60,
        commands: aoaPanelState.commands,
        metadata: aoaHidMetadata(),
    };
    const url = URL.createObjectURL(new Blob(
        [JSON.stringify(payload, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'aoa-profile.json';
    link.click(); URL.revokeObjectURL(url);
}

function aoaCommandsImport() {
    if (aoaPanelState.playbackActive) return; aoaEl('aoa-command-file')?.click(); }

async function aoaImportFileChanged(event) {
    if (aoaPanelState.playbackActive) return;
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file) return;
    try {
        const parsed = JSON.parse(await file.text());
        const commands = Array.isArray(parsed) ? parsed : parsed.commands;
        if (!Array.isArray(commands) || !commands.length || commands.length > 256
                || commands.some(item => typeof item !== 'string' || !item.trim())) {
            throw new Error('JSON 必须包含 1..256 条非空 commands');
        }
        aoaPanelState.commands = commands.map(item => item.trim());
        aoaLoadHidMetadata(Array.isArray(parsed) ? {} : parsed.metadata || {});
        aoaPanelState.commandEdit = null;
        aoaPanelState.editingProfileId = '';
        aoaPanelState.commandsProfileId = '';
        if (!Array.isArray(parsed) && parsed.name) {
            aoaEl('aoa-profile-name').value = String(parsed.name).slice(0, 120);
        }
        if (!Array.isArray(parsed) && parsed.wait_adb_seconds) {
            aoaEl('aoa-replay-wait').value = String(parsed.wait_adb_seconds);
        }
        aoaRenderCommands();
    } catch (error) { showToast(`导入失败：${error.message}`, 'warning'); }
}

// 录制器执行/投屏交互/触摸标定：见 aoa-recorder.js（体积 ratchet 拆分）。
function aoaPointFromPointer(event) {
    // 实时投屏激活时以 canvas 为点击面，否则截图 img。
    const liveActive = Boolean(window.aoaLiveScreen?.isActive());
    const canvas = liveActive ? aoaEl('aoa-screen-canvas') : null;
    const image = aoaEl('aoa-screen-image');
    const surface = (canvas && !canvas.hidden) ? canvas : image;
    if (!surface || surface.hidden) return null;
    const rect = surface.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const naturalW = (surface === canvas) ? canvas.width : image.naturalWidth;
    const naturalH = (surface === canvas) ? canvas.height : image.naturalHeight;
    if (!naturalW || !naturalH) return null;
    const x = Math.round((event.clientX - rect.left) * naturalW / rect.width);
    const y = Math.round((event.clientY - rect.top) * naturalH / rect.height);
    return {x: Math.max(0, Math.min(naturalW, x)),
        y: Math.max(0, Math.min(naturalH, y))};
}

function aoaPointerDown(event) {
    if (aoaPanelState.running) return;
    // 实时投屏 canvas 或截图 img 任一可见即可操作。
    const liveOn = aoaLiveInputSurfaceActive();
    const canvasOn = liveOn && !aoaEl('aoa-screen-canvas')?.hidden;
    const imageOn = !aoaEl('aoa-screen-image')?.hidden;
    if (!canvasOn && !imageOn) return;
    const point = aoaPointFromPointer(event); if (!point) return;
    event.preventDefault();
    if (event.currentTarget.setPointerCapture) {
        event.currentTarget.setPointerCapture(event.pointerId);
    }
    aoaPanelState.pointer = {id: event.pointerId, startedAt: performance.now(),
        clientX: event.clientX, clientY: event.clientY, point};
}

function aoaPointerMove(event) {
    const pointer = aoaPanelState.pointer;
    if (!pointer || pointer.id !== event.pointerId) return;
    const stage = aoaEl('aoa-screen-stage').getBoundingClientRect();
    const dx = event.clientX - pointer.clientX; const dy = event.clientY - pointer.clientY;
    const line = aoaEl('aoa-gesture-line'); line.hidden = false;
    line.style.left = `${pointer.clientX - stage.left}px`;
    line.style.top = `${pointer.clientY - stage.top}px`;
    line.style.width = `${Math.hypot(dx, dy)}px`;
    line.style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
}

function aoaPointerUp(event) {
    const pointer = aoaPanelState.pointer;
    if (!pointer || pointer.id !== event.pointerId) return;
    aoaPanelState.pointer = null; aoaEl('aoa-gesture-line').hidden = true;
    const end = aoaPointFromPointer(event) || pointer.point;
    const distance = Math.hypot(event.clientX - pointer.clientX, event.clientY - pointer.clientY);
    const elapsed = Math.round(performance.now() - pointer.startedAt);
    if (distance < 9 && elapsed >= 650) {
        aoaExecuteRecorderInput({action: 'long_click', x: pointer.point.x, y: pointer.point.y,
            duration_ms: Math.min(Math.max(elapsed, 650), 3000)});
    } else if (distance < 9) {
        aoaExecuteRecorderInput({action: 'tap', x: pointer.point.x, y: pointer.point.y});
    } else {
        aoaExecuteRecorderInput({action: 'swipe', x1: pointer.point.x, y1: pointer.point.y,
            x2: end.x, y2: end.y, duration_ms: Math.min(Math.max(elapsed, 150), 2000)});
    }
}

async function aoaSaveRecording() {
    if (aoaPanelState.running || !aoaCommandEditFinish()) return;
    const name = aoaEl('aoa-profile-name')?.value.trim();
    if (!name || !aoaPanelState.commands.length) {
        showToast('请填写配置名称，并至少录制一条命令', 'warning'); return;
    }
    const body = {name, commands: aoaPanelState.commands,
        wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || 60,
        metadata: {...aoaHidMetadata(), calibration: aoaPanelState.calibration},
        description: `Web AOA interactive recording from ${aoaEl('aoa-record-device')?.value || 'device'}`};
    let targetId = aoaPanelState.editingProfileId;
    if (!targetId) {
        // 导入/新录制保存时，同名配置先确认覆盖再转 PUT 更新，
        // 避免直接 POST 撞唯一约束报 409 同名冲突。
        const existing = aoaPanelState.profiles.find(profile => profile.name === name);
        if (existing) {
            const ok = await showConfirmDialog('覆盖 AOA 配置',
                `已存在同名配置“${name}”，保存将覆盖其命令与 ADB 等待时间，是否继续？`);
            if (!ok) return;
            targetId = existing.id;
        }
    }
    const path = targetId
        ? `/api/devices/aoa/profiles/${encodeURIComponent(targetId)}`
        : '/api/devices/aoa/profiles/from-recording';
    try {
        const result = await aoaApi(path, {method: targetId ? 'PUT' : 'POST', body});
        aoaPanelState.editingProfileId = result.profile.id;
        aoaPanelState.commandsProfileId = result.profile.id;
        showToast(`已保存 AOA 配置：${result.profile.name}`, 'success');
        await aoaLoadPanel(); aoaEl('aoa-replay-profile').value = result.profile.id;
        aoaProfileSelectionChanged();
    } catch (error) { showToast(`保存失败：${error.message}`, 'warning'); }
}

async function aoaProfileDeleteSelected() {
    const profile = aoaSelectedProfile(); if (!profile) return;
    const ok = await showConfirmDialog('删除 AOA 配置', `确定删除“${profile.name}”？`);
    if (!ok) return;
    try {
        await aoaApi(`/api/devices/aoa/profiles/${encodeURIComponent(profile.id)}`, {method: 'DELETE'});
        if (aoaPanelState.editingProfileId === profile.id) aoaPanelState.editingProfileId = '';
        await aoaLoadPanel(); showToast('AOA 配置已删除', 'success');
    } catch (error) { showToast(`删除失败：${error.message}`, 'warning'); }
}

function aoaShowResult(title, payload) {
    const box = aoaEl('aoa-result'); if (!box) return;
    const result = payload?.entries?.[0] || payload || {}; const parts = [title];
    if (result.commands_executed !== undefined) parts.push(`命令 ${result.commands_executed}/${result.commands_total}`);
    if (result.adb_recovered !== undefined) {
        parts.push(`ADB ${result.adb_recovered ? '已恢复' : `未恢复（${result.adb_state || 'absent'}）`}`);
    }
    if (result.error) parts.push(`错误：${result.error}`);
    box.textContent = parts.join(' · '); box.hidden = false;
}

async function aoaReplaySelected(profileId = '') {
    if (aoaPanelState.running) return;
    if (!aoaCommandEditFinish()) return;
    const worker = aoaEl('aoa-replay-worker')?.value;
    const device = aoaEl('aoa-replay-device')?.value;
    const profile = profileId ? aoaPanelState.profiles.find(item => item.id === profileId) : aoaSelectedProfile();
    if (!worker || !device || !profile) { showToast('请选择目标 Worker、设备和 AOA 配置', 'warning'); return; }
    const commands = [...(aoaPanelState.commandsProfileId === profile.id
        ? aoaPanelState.commands : profile.commands || [])].map(command => String(command).trim()).filter(Boolean);
    if (!commands.length) { showToast('当前命令列表为空，请先添加命令', 'warning'); return; }
    const hidMetadata = (aoaPanelState.commandsProfileId === profile.id || aoaPanelState.hidSettingsProfileId === profile.id)
        ? aoaHidMetadata() : profile.metadata || {};
    const ok = await showConfirmDialog('AOA 回放',
        `将在 ${device} 上通过 USB 回放“${profile.name}”（${commands.length} 条），期间设备会短暂断开 ADB。继续？`);
    if (!ok || aoaPanelState.running) return;
    if (aoaPanelState.recording) aoaRecordingStop();
    aoaLoadHidMetadata(hidMetadata);
    // 回放前把该配置的完整命令渲染进 ③ 列表：页面刷新后编辑器可能为空，
    // 而回放进度高亮（aoaHighlightCommandRow）依赖列表行存在才能滚动定位。
    aoaPanelState.commandDebug = null;
    aoaPanelState.playbackActive = true;
    aoaPanelState.playbackInputDone = false;
    aoaPanelState.playbackStates = commands.map(() => 'pending');
    aoaPanelState.playbackIndex = 0;
    aoaPanelState.commands = [...commands];
    aoaPanelState.editingProfileId = profile.id;
    aoaPanelState.commandsProfileId = profile.id;
    aoaRenderCommands();
    // 过程控制句柄：一次性随机 id，随 /recover 下发到执行线程；
    // 运行中经 /playback/{id}/control 暂停/继续/停止。
    aoaPanelState.playbackControlId =
        `pb-${crypto.randomUUID()}`;
    aoaPanelState.playbackPaused = false;
    aoaPanelState.playbackWorker = worker;
    aoaSetBusy(true, `回放中：${profile.name}`);
    try {
        let result = await aoaApi('/api/devices/aoa/recover', {method: 'POST', body: {
            worker_id: worker, device, commands,
            metadata: hidMetadata,
            wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || profile.wait_adb_seconds || 60,
            playback_control_id: aoaPanelState.playbackControlId,
        }});
        aoaPanelState.playbackCommandId = result.command_id || '';
        aoaPlaybackButtonState();
        result = await aoaAwaitCommand(result);
        aoaShowResult(`回放完成：${profile.name}`, result);
        aoaSetStatus(result?.success === false ? '回放未完成' : '回放完成',
            result?.success === false ? 'error' : 'success');
    } catch (error) {
        aoaShowResult(`回放失败：${profile.name}`, {error: error.message});
        aoaSetStatus(`回放失败：${error.message}`, 'error');
    } finally {
        aoaPlaybackReset();
        aoaSetBusy(false);
    }
}

async function aoaProbeStart() {
    const worker = aoaEl('aoa-replay-worker')?.value;
    const device = aoaEl('aoa-replay-device')?.value;
    if (!worker || !device) { showToast('请选择目标 Worker 与设备', 'warning'); return; }
    aoaSetBusy(true, '正在探测 AOAv2…');
    try {
        let result = await aoaApi('/api/devices/aoa/probe', {method: 'POST', body: {worker_id: worker, device}});
        result = await aoaAwaitCommand(result);
        const supported = result.aoa_supported === true || Number(result.aoa_protocol) >= 2;
        showToast(`AOA 探测：${supported ? `支持协议 v${result.aoa_protocol}` : '不支持 AOAv2'}`,
            supported ? 'success' : 'warning');
        aoaSetStatus(supported ? `AOAv2 v${result.aoa_protocol}` : 'AOAv2 不可用',
            supported ? 'success' : 'error');
    } catch (error) {
        showToast(`AOA 探测失败：${error.message}`, 'warning'); aoaSetStatus(`探测失败：${error.message}`, 'error');
    } finally { aoaSetBusy(false); }
}

function aoaViewIsOpen() { const panel = aoaEl('aoa-panel'); return Boolean(panel) && !panel.hidden; }

async function aoaViewToggle(show) {
    const panel = aoaEl('aoa-panel'); const main = aoaEl('devices-main-view');
    if (!panel || !main) return;
    const visible = show === undefined ? !aoaViewIsOpen() : Boolean(show);
    panel.hidden = !visible; main.hidden = visible; aoaEl('device-group-toolbar').hidden = visible;
    aoaEl('devices-list-tab')?.classList.toggle('active', !visible);
    aoaEl('aoa-tab')?.classList.toggle('active', visible);
    aoaEl('devices-list-tab')?.setAttribute('aria-selected', String(!visible));
    aoaEl('aoa-tab')?.setAttribute('aria-selected', String(visible));
    if (visible) {
        if (typeof stopDevicesAutoRefresh === 'function') stopDevicesAutoRefresh();
        await aoaLoadPanel();
        // 清单同步后按当前选中设备联动：默认自动连接实时投屏（无选中
        // 设备则只显示空态提示），与切换设备时的自动续流行为一致。
        if (typeof loadDevicesManagement === 'function') await loadDevicesManagement();
        if (typeof stopDevicesAutoRefresh === 'function') stopDevicesAutoRefresh();
        aoaRecorderDeviceChanged();
    } else {
        if (aoaDebugState.id) await aoaDebugEnd();
        aoaPanelState.screenRefreshId += 1;
        // 离开 AOA 页签：停止实时投屏与截图轮询。
        if (window.aoaLiveScreen) {
            window.aoaLiveScreen.stop();
            aoaEl('aoa-toggle-live')?.setAttribute('data-live', 'off');
        }
        if (typeof currentPage === 'undefined' || currentPage === 'devices') {
            if (typeof loadDevicesManagement === 'function') loadDevicesManagement();
        }
    }
}

function aoaInitializeWorkbench() {
    const image = aoaEl('aoa-screen-image');
    if (image && !image.dataset.aoaInitialized) {
        image.dataset.aoaInitialized = '1';
        image.addEventListener('pointerdown', aoaPointerDown);
        image.addEventListener('pointermove', aoaPointerMove);
        image.addEventListener('pointerup', aoaPointerUp);
        image.addEventListener('pointercancel', aoaPointerUp);
    }
    const canvas = aoaEl('aoa-screen-canvas');
    if (canvas && !canvas.dataset.aoaInitialized) {
        canvas.dataset.aoaInitialized = '1';
        canvas.addEventListener('pointerdown', aoaPointerDown);
        canvas.addEventListener('pointermove', aoaPointerMove);
        canvas.addEventListener('pointerup', aoaPointerUp);
        canvas.addEventListener('pointercancel', aoaPointerUp);
    }
    const file = aoaEl('aoa-command-file');
    if (file && !file.dataset.aoaInitialized) {
        file.dataset.aoaInitialized = '1'; file.addEventListener('change', aoaImportFileChanged);
    }
    aoaRenderCommands(); aoaRenderCalibration();
    aoaSetBusy(aoaPanelState.running);
}

// 兼容初版 AOA 页签导出的调用名。
function aoaRecordStart() { return aoaRecordingStart(); }
function aoaProfileReplay(profileId) { return aoaReplaySelected(profileId); }
function aoaProfileDelete() { return aoaProfileDeleteSelected(); }
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', aoaInitializeWorkbench, {once: true});
} else { aoaInitializeWorkbench(); }
