// ==================== Web AOA 三列工作台（设备管理页） ====================
// 左：ADB 截图投屏与 UI 输入；中：AOA 命令与注释；右：录制/Profile/目标回放。
// 真正的输入和回放都经 Controller 的设备 claim/fencing → Worker 边界执行，
// 本文件不修改 Test Workspace 的 worker/device 选择。
'use strict';

const AOA_TOUCH_CALIBRATION_VERSION = 3;

const aoaPanelState = {
    profiles: [], devices: [], commands: [], loaded: false, loading: false,
    running: false, recording: false, editingProfileId: '', screenRefreshId: 0,
    calibration: null, pointer: null, liveTargetKey: '',
    profileMetadata: {},
    hidSettingsProfileId: '',
    playbackControlId: '', playbackPaused: false, playbackActive: false,
    playbackCommandId: '', playbackWorker: '', playbackPending: '', playbackInputDone: false,
    playbackStates: [], playbackIndex: 0, commandDebug: null, commandEdit: null, commentEdit: null, commandsProfileId: '',
    commandCalibrate: null,   // 命令坐标标定模式：{index, original, x, y}
};

function aoaEl(id) { return document.getElementById(id); }

function aoaHidMetadata() {
    return {...aoaPanelState.profileMetadata,
        hid_touch_mode: aoaEl('aoa-hid-touch-mode')?.value || 'direct',
        hid_session_mode: aoaEl('aoa-hid-session-mode')?.value || 'continuous',
        hid_settle_seconds: Number(aoaEl('aoa-hid-settle')?.value || 2)};
}

function aoaLoadHidMetadata(metadata = {}, commandCount = null) {
    aoaValidateCommandComments(metadata, commandCount);
    const context = metadata.calibration?.context;
    const inferred = Number.isInteger(context?.version) && context.version >= 2
        ? context.hid_touch_mode || 'direct' : 'pointer';
    const mode = metadata.hid_touch_mode || metadata.calibration?.hid_touch_mode || inferred;
    const settle = metadata.hid_settle_seconds ?? 2;
    const sessionMode = metadata.hid_session_mode || 'continuous';
    if (!['pointer', 'direct'].includes(mode) || typeof settle !== 'number'
            || !Number.isFinite(settle) || settle < 0 || settle > 10
            || !['continuous', 'per_command'].includes(sessionMode)) {
        throw new Error('AOA 输入模式或稳定等待无效，请检查配置');
    }
    aoaPanelState.profileMetadata = {...metadata};
    aoaPanelState.commentEdit = null;
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
        const comments = aoaPanelState.profileMetadata.command_comments;
        aoaLoadHidMetadata(profile.metadata || {});
        if (comments !== undefined) aoaPanelState.profileMetadata.command_comments = comments;
        else delete aoaPanelState.profileMetadata.command_comments;
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
            // 结构化失败标记（如 calibration_app_missing）随错误对象透传，
            // 调用方据此给出可执行的引导而不是裸错误文案。
            const detail = payload.detail || payload.error || {};
            for (const key of ['calibration_app_missing', 'reconcile_required']) {
                if (detail && typeof detail === 'object' && detail[key]) error[key] = true;
            }
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
    document.querySelectorAll('#aoa-panel [data-aoa-run], #aoa-panel [data-aoa-screen-action], #aoa-panel select, #aoa-panel [data-aoa-edit], #aoa-command-list button, #aoa-command-list input, #aoa-command-list textarea, #aoa-command-input, #aoa-record-start, #aoa-record-stop, #aoa-profile-name, #aoa-replay-wait')
        .forEach(node => { node.disabled = running; });
    if (text) aoaSetStatus(text, running ? 'running' : '');
    if (aoaEl('aoa-record-start')) aoaEl('aoa-record-start').disabled = running || aoaPanelState.recording;
    // 停止按钮合一：录制中/回放中都可点（分别走录制停止/回放取消），
    // 由 aoaPlaybackButtonState 在回放态统一管理 disabled。
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

function aoaDeviceContext(serial, workerId) {
    const device = aoaPanelState.devices.find(item =>
        aoaDeviceSerial(item) === String(serial) && aoaDeviceWorker(item) === String(workerId));
    if (!device) return null;
    const properties = {...(device.properties || {}), ...(device.aoa_context || device.properties?.aoa_context || {})};
    const model = device.model || properties.model || '';
    const release = device.android_version || device.release || properties.android_version
        || properties.release || '';
    const rawDisplay = device.display || device.display_pixels || properties.display
        || properties.display_pixels;
    const display = Array.isArray(rawDisplay) && rawDisplay.length === 2
        ? rawDisplay.map(Number) : null;
    const rotation = Number.isInteger(device.rotation) ? device.rotation
        : Number.isInteger(properties.rotation) ? properties.rotation : null;
    if (!model || !release || !display || display.some(value => !Number.isInteger(value) || value <= 0)
            || !Number.isInteger(rotation) || rotation < 0 || rotation > 3) return null;
    return {model: String(model), release: String(release), display, rotation,
        fingerprint: properties.fingerprint || device.fingerprint || '',
        density: properties.density ?? device.density ?? null,
        kernel: properties.kernel || device.kernel || '',
        display_route: properties.display_route || device.display_route || ''};
}

function aoaReplaceOptions(select, entries, emptyLabel, keepMissing = false) {
    if (!select) return;
    const previous = select.value;
    if (keepMissing && previous && !entries.some(entry => entry.value === previous)) {
        const label = select.selectedOptions[0]?.dataset.label || previous;
        entries = [{value: previous, label, missing: true}, ...entries];
    }
    if (entries.length && entries.length === select.options.length && entries.every((entry, index) => {
        const option = select.options[index];
        return option.value === entry.value && option.dataset.label === entry.label
            && option.textContent === entry.label + (entry.missing ? '（暂未枚举）' : '');
    })) return;
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
    aoaFillDeviceSelect('aoa-record-device', aoaEl('aoa-record-worker')?.value || '', true, true);
    if (!aoaDebugSyncTarget()) aoaFillDeviceSelect('aoa-replay-device', aoaEl('aoa-replay-worker')?.value || '', false, true);
}

// 截图轮询暂停标记按设备状态驱动：目标设备无 ADB（usb_only/fastboot/
// 离线/不在清单）时直接暂停（零请求）；清单刷新或目标切换后按最新
// 状态复位。不能盲目清标记——设备页每 10s 自动刷新会反复清除，随后
// 轮询又打一轮 409（usb_only 设备的截图必然被拒）。
function aoaSyncAdbPauseFlag() {
    const workerId = aoaEl('aoa-record-worker')?.value || '';
    const serial = aoaEl('aoa-record-device')?.value || '';
    const device = aoaPanelState.devices.find(item =>
        aoaDeviceWorker(item) === workerId && aoaDeviceSerial(item) === serial);
    const deviceState = String(
        device?.cluster_state || device?.state || device?.status || ''
    ).toLowerCase();
    aoaPanelState.adbUnavailable = !device
        || ['usb_only', 'fastboot', 'offline', 'unknown'].includes(deviceState);
    // 占用暂停（烧写/GSI fencing 409）随清单同步复位：允许重试一次，
    // 仍被占用则由下一轮 409 重新暂停（每轮清单至多 1 次请求）。
    aoaPanelState.screenBusyPaused = false;
}

// 设备管理清单刷新后的只读同步入口。
function aoaSyncDeviceSources(devices) {
    if (!aoaEl('aoa-panel')) return;
    aoaPanelState.devices = Array.isArray(devices) ? devices : [];
    const workers = [...new Set(aoaPanelState.devices
        .filter(device => device.transport !== 'adb_proxy')
        .map(aoaDeviceWorker).filter(Boolean))];
    const entries = workers.map(worker => ({value: worker, label: worker}));
    aoaReplaceOptions(aoaEl('aoa-record-worker'), entries, '（无可用 Worker）', true);
    aoaReplaceOptions(aoaEl('aoa-replay-worker'), entries, '（无可用 Worker）', true);
    aoaSyncDeviceOptions();
    // 下拉就绪后按最新清单状态决定是否暂停截图轮询
    // （usb_only → 暂停；ADB 就绪 → 恢复）。必须在填充之后：否则首次
    // 同步时 select 为空、查不到目标，会误置暂停标记。
    aoaSyncAdbPauseFlag();
}

function aoaRecorderWorkerChanged() {
    aoaFillDeviceSelect('aoa-record-device', aoaEl('aoa-record-worker')?.value || '', true);
    aoaRecorderDeviceChanged();
}

function aoaReplayWorkerChanged() {
    if (aoaDebugSyncTarget()) return;
    aoaFillDeviceSelect('aoa-replay-device', aoaEl('aoa-replay-worker')?.value || '', false);
}

// 用户在录制设备下拉里明确选择设备：这是唯一的自动连接实时投屏入口。
function aoaRecorderDeviceSelected() {
    aoaRecorderDeviceChanged({autoLive: true});
}

function aoaRecorderDeviceChanged(options = {}) {
    aoaPanelState.calibration = null;
    // 切换目标设备：按新目标在清单里的状态重设截图暂停标记。
    aoaSyncAdbPauseFlag();
    const key = `gms-aoa-calibration:${aoaEl('aoa-record-worker')?.value || ''}:${aoaEl('aoa-record-device')?.value || ''}`;
    try {
        const saved = JSON.parse(localStorage.getItem(key) || 'null');
        if (saved?.context?.version === AOA_TOUCH_CALIBRATION_VERSION
                && (saved.context.hid_touch_mode || 'direct') === aoaHidMetadata().hid_touch_mode) {
            aoaPanelState.calibration = saved;
        }
    } catch (_) { /* storage unavailable */ }
    aoaRenderCalibration();
    // 只读联动（打开页签/清单同步/切换 Worker）不得发起变更请求：目标
    // 未变化时保持现有投屏不中断；目标变化时仅停止旧会话，等待用户在
    // 下拉里明确选择设备后再自动重连（前端初始化/默认值路径零变更写）。
    const targetKey = `${aoaEl('aoa-record-worker')?.value || ''}:${aoaEl('aoa-record-device')?.value || ''}`;
    const targetChanged = targetKey !== aoaPanelState.liveTargetKey;
    aoaPanelState.liveTargetKey = targetKey;
    const target = aoaLiveTarget();
    if (target && !targetChanged && !options.autoLive && window.aoaLiveScreen?.isActive()) {
        // Preserve the visible stream on read-only workbench re-entry.
        return;
    }
    const image = aoaEl('aoa-screen-image');
    if (image) { image.hidden = true; image.removeAttribute('src'); }
    const canvas = aoaEl('aoa-screen-canvas');
    if (canvas) { canvas.hidden = true; }
    const empty = aoaEl('aoa-screen-empty');
    if (empty) { empty.hidden = true; }
    if (!target) {
        if (window.aoaLiveScreen?.isActive()) {
            window.aoaLiveScreen.stop();
            aoaEl('aoa-toggle-live')?.setAttribute(
                'data-live', 'off');
            aoaSyncLiveButton();
        }
        if (empty) empty.hidden = false;
        const status = aoaEl('aoa-screen-status');
        if (status) status.textContent = '选择录制设备后点「▶ 实时投屏」连接';
        aoaRenderCalibration();
        return;
    }
    if (options.autoLive) {
        // 用户明确选择设备：自动优先实时流（scrcpy）；流失败由
        // aoaLiveScreen.start 内部回退截图轮询。「▶」按钮仅作为开关。
        if (window.aoaLiveScreen?.isActive()) {
            window.aoaLiveScreen.stop();
        }
        const status = aoaEl('aoa-screen-status');
        if (status) status.textContent = `正在连接 ${target.device} 实时投屏…`;
        window.aoaLiveScreen.start(target.device, target.workerId).then(() => {
            aoaEl('aoa-toggle-live')?.setAttribute(
                'data-live', window.aoaLiveScreen.isActive() ? 'on' : 'off');
            aoaSyncLiveButton();
        });
        aoaRenderCalibration();
        return;
    }
    if (targetChanged && window.aoaLiveScreen?.isActive()) {
        window.aoaLiveScreen.stop();
        aoaEl('aoa-toggle-live')?.setAttribute(
            'data-live', 'off');
        aoaSyncLiveButton();
    }
    const status = aoaEl('aoa-screen-status');
    if (status) status.textContent = `点「▶ 实时投屏」连接 ${target.device}`;
    aoaEl('aoa-toggle-live')?.setAttribute(
        'data-live', 'off');
    aoaSyncLiveButton();
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
    node.title = node.textContent;
}

function aoaLoadSelectedProfile() {
    if (aoaPanelState.playbackActive) return;
    const profile = aoaSelectedProfile();
    if (!profile) { showToast('请先选择一个 AOA 配置', 'warning'); return; }
    const commands = (profile.commands || []).map(command => String(command).trim()).filter(Boolean);
    try { aoaValidateCommandComments(profile.metadata || {}, commands.length); }
    catch (error) { aoaSetStatus(`载入失败：${error.message}`, 'error'); return; }
    aoaPlaybackReset();
    aoaPanelState.commandDebug = null;
    aoaPanelState.commandEdit = null;
    aoaPanelState.playbackStates = [];
    aoaPanelState.commands = [...commands];
    aoaPanelState.editingProfileId = profile.id;
    aoaPanelState.commandsProfileId = profile.id;
    aoaEl('aoa-profile-name').value = profile.name;
    aoaLoadHidMetadata(profile.metadata || {}, commands.length);
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
    aoaCommandCalibrateStale();   // 命令已变/列表重置：自动清准星并按现状渲染
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
        code.textContent = command; code.title = command;
        const semantic = aoaPanelState.profileMetadata?.semantic_template?.steps[i];
        if (semantic) code.title = `${command}\n语义目标：${semantic.target || [semantic.from_target, semantic.to_target].filter(Boolean).join(' → ') || command}；触摸坐标由匹配布局计算，执行前后验证状态`;
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
        const locate = document.createElement('button');
        locate.className = 'btn-xxs aoa-command-locate'; locate.type = 'button'; locate.textContent = '定位';
        locate.disabled = aoaPanelState.running || aoaPanelState.playbackActive;
        // 标定模式：不执行命令、不碰设备。在投屏上显示该命令坐标的准星，
        // 点击画面把准星（命令坐标）挪到想要的点位，再点「保存」改写命令。
        // AOA HID 是触摸屏，没有"悬停移动"报文，设备端无法只移动不点击；
        // 可视化定位都发生在投屏叠加层上（aoa-screen-grid.js）。
        locate.title = '投屏上显示该命令的坐标准星；点击画面调整位置，保存后改写命令坐标（不执行）';
        locate.setAttribute('aria-label', `定位第 ${i + 1} 条命令坐标`);
        locate.addEventListener('click', () => aoaPanelState.commandCalibrate?.index === i
            ? aoaCommandCalibrateFinish() : aoaCommandCalibrateStart(i));
        const actionButtons = [edit, execute, locate];
        if (aoaPanelState.commandCalibrate?.index === i) {
            // 标定态 5 个按钮（编辑/执行/保存/取消/删除）超出第 5 列固定
            // 156px：与编辑模式同规则跨到状态列（4 / 6），避免删除被裁掉。
            // class 加在 .aoa-command-row（content）上，与 CSS 选择器同元素。
            content.classList.add('aoa-command-calibrating');
            locate.textContent = '保存';
            locate.title = '把准星坐标写回这条命令（click X Y）';
            locate.setAttribute('aria-label', `保存第 ${i + 1} 条命令标定坐标`);
            const cancelCal = document.createElement('button');
            cancelCal.className = 'btn-xxs'; cancelCal.type = 'button'; cancelCal.textContent = '取消';
            cancelCal.disabled = locate.disabled;
            cancelCal.setAttribute('aria-label', `取消第 ${i + 1} 条命令标定`);
            cancelCal.addEventListener('click', () => { aoaCommandCalibrateCancel(); aoaRenderCommands(); });
            actionButtons.push(cancelCal);
        }
        const remove = document.createElement('button');
        remove.className = 'btn-xxs'; remove.type = 'button'; remove.textContent = '删除';
        remove.disabled = aoaPanelState.running;
        remove.addEventListener('click', () => {
            if (aoaPanelState.running) return;
            if (!aoaCommandEditFinish()) return;
            aoaPanelState.commands.splice(i, 1);
            aoaSyncCommandComments(i);
            aoaPanelState.editingProfileId = '';
            aoaRenderCommands();
        });
        const state = document.createElement('span'); state.className = 'aoa-command-state';
        state.hidden = !aoaPanelState.playbackActive;
        const actions = document.createElement('div'); actions.className = 'aoa-command-actions';
        actions.append(...actionButtons, remove);
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
        content.append(index, commandNode, aoaCreateCommandComment(i), state, actions); row.append(content); list.append(row);
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
    if (aoaPanelState.running || aoaPanelState.playbackActive) return;
    const command = aoaPanelState.commands[index];
    if (typeof command !== 'string') return;
    aoaCommandCalibrateCancel();   // 标定与编辑互斥：进编辑先撤准星（不保存）
    if (!aoaCommandEditFinish()) return;
    const list = aoaEl('aoa-command-list'); const scroll = list.scrollTop;
    aoaPanelState.commandEdit = {index, original: command, draft: command};
    aoaRenderCommands(); list.scrollTop = scroll;
    const input = list.querySelector('.aoa-command-editor'); input.focus(); input.select();
}

function aoaCommandEditFinish(save = true) {
    if (!aoaCommentEditFinish(save)) return false;
    aoaCommandCalibrateCancel();   // 标定与编辑互斥：收口命令编辑时撤准星
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
    aoaSyncCommandComments();
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
    aoaResetCommandComments();
    delete aoaPanelState.profileMetadata.calibration;
    aoaPanelState.commandEdit = null;
    aoaPanelState.commandsProfileId = '';
    aoaPanelState.recording = true;
    aoaEl('aoa-record-start').disabled = true;
    // 停止按钮立即可点：启动路径可能没有 aoaSetBusy 周期来触发
    // aoaPlaybackButtonState 解禁（空闲态上次 busy 周期会把它禁用）。
    if (aoaEl('aoa-record-stop')) aoaEl('aoa-record-stop').disabled = false;
    aoaEl('aoa-record-state').textContent = '录制中：左侧成功操作会写入命令';
    aoaSetStatus('录制中', 'running'); aoaRenderCommands();
    if (aoaEl('aoa-screen-image')?.hidden) await aoaRefreshScreen({force: true});
}

function aoaRecordingStop() {
    aoaPanelState.recording = false;
    if (aoaEl('aoa-record-start')) aoaEl('aoa-record-start').disabled = false;
    // 停止后回到空闲态：可点性交给 aoaPlaybackButtonState 的录制语义
    // （!recording → disabled），这里显式复位避免中间态漂移。
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
    aoaSyncCommandComments();
    aoaRenderCommands();
}

async function aoaCommandsClear() {
    if (aoaPanelState.playbackActive) return;
    if (!aoaPanelState.commands.length) return;
    const ok = await showConfirmDialog('清空 AOA 命令', '确定清空当前命令轨道？');
    if (!ok) return;
    aoaPanelState.commands = []; aoaPanelState.editingProfileId = '';
    aoaResetCommandComments();
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
        const metadata = Array.isArray(parsed) ? {} : parsed.metadata || {};
        aoaValidateCommandComments(metadata, commands.length);
        aoaPanelState.commands = commands.map(item => item.trim());
        aoaLoadHidMetadata(metadata, commands.length);
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
async function aoaSaveRecording() {
    if (aoaPanelState.running || !aoaCommandEditFinish()) return;
    const name = aoaEl('aoa-profile-name')?.value.trim();
    if (!name || !aoaPanelState.commands.length) {
        showToast('请填写配置名称，并至少录制一条命令', 'warning'); return;
    }
    const body = {name, commands: aoaPanelState.commands,
        wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || 60,
        metadata: {...aoaHidMetadata(), calibration: aoaPanelState.calibration ?? aoaHidMetadata().calibration ?? null},
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
        aoaSetStatus(`已保存 AOA 配置：${result.profile.name}`, 'success');
        await aoaLoadPanel(); aoaEl('aoa-replay-profile').value = result.profile.id;
        aoaProfileSelectionChanged();
    } catch (error) {
        aoaSetStatus(`保存失败：${error.message}`, 'error');
        showToast(`保存失败：${error.message}`, 'warning');
    }
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
    aoaLoadHidMetadata(hidMetadata, commands.length);
    // 回放前把该配置的完整命令渲染进 ② 列表：页面刷新后编辑器可能为空，
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
    const deviceContext = aoaDeviceContext(device, worker);
    try {
        let result = await aoaApi('/api/devices/aoa/recover', {method: 'POST', body: {
            worker_id: worker, device, commands,
            metadata: hidMetadata,
            ...(deviceContext ? {device_context: deviceContext} : {}),
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
    // 页签状态随 F5 刷新恢复（页面初始化经 runPageInitializers 读取）。
    try { sessionStorage.setItem('gms_devices_view', visible ? 'aoa' : 'list'); } catch (_error) {}
    if (visible) {
        await aoaLoadPanel();
        // 只读联动：刷新下拉与画面状态，不自动发起投屏（变更请求仅跟随
        // 用户明确选择设备或「▶ 实时投屏」开关，零初始化写）。
        if (typeof loadDevicesManagement === 'function') await loadDevicesManagement();
        aoaRecorderDeviceChanged();
    } else {
        if (aoaDebugState.id) await aoaDebugEnd();
        aoaPanelState.screenRefreshId += 1;
        // 离开 AOA 页签：结束标定取点、停止实时投屏与截图轮询。
        aoaCommandCalibrateCancel();
        if (window.aoaLiveScreen) {
            window.aoaLiveScreen.stop();
            aoaEl('aoa-toggle-live')?.setAttribute(
                'data-live', 'off');
            aoaSyncLiveButton();
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
        image.addEventListener('pointercancel', aoaPointerCancel);
    }
    const canvas = aoaEl('aoa-screen-canvas');
    if (canvas && !canvas.dataset.aoaInitialized) {
        canvas.dataset.aoaInitialized = '1';
        canvas.addEventListener('pointerdown', aoaPointerDown);
        canvas.addEventListener('pointermove', aoaPointerMove);
        canvas.addEventListener('pointerup', aoaPointerUp);
        canvas.addEventListener('pointercancel', aoaPointerCancel);
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
