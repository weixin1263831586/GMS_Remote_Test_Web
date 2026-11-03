// ==================== Web AOA 三列工作台（设备管理页） ====================
// 左：ADB 截图投屏与 UI 输入；中：录制/Profile/目标回放；右：AOA 命令轨道。
// 真正的输入和回放都经 Controller 的设备 claim/fencing → Worker 边界执行，
// 本文件不修改 Test Workspace 的 worker/device 选择。
'use strict';

const aoaPanelState = {
    profiles: [], devices: [], commands: [], loaded: false, loading: false,
    running: false, recording: false, editingProfileId: '', screenRefreshId: 0,
    calibration: null, pointer: null, autoConnectDone: false,
};

function aoaEl(id) { return document.getElementById(id); }

function aoaMessage(error) {
    const detail = error?.detail || error?.error || error;
    if (typeof detail === 'string') return detail;
    return detail?.message || detail?.detail || JSON.stringify(detail || '未知错误');
}

async function aoaApi(path, options = {}) {
    const request = {...options, headers: {...(options.headers || {})}};
    if (request.body && typeof request.body !== 'string') {
        request.headers['Content-Type'] = 'application/json';
        request.body = JSON.stringify(request.body);
    }
    const response = await fetch(path, request);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.success === false) {
        throw new Error(aoaMessage(payload) || `HTTP ${response.status}`);
    }
    return payload.data || payload;
}

async function aoaAwaitCommand(payload) {
    if (!payload?.accepted) return payload;
    const commandId = payload.command_id;
    if (!commandId) throw new Error('Worker 已接受操作但未返回 command_id');
    const deadline = Date.now() + 200000;
    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const status = await aoaApi(`/api/cluster/commands/${encodeURIComponent(commandId)}`);
        const command = status.command || {};
        if (command.status === 'completed') return command.result || {};
        if (['failed', 'cancelled'].includes(command.status)) {
            throw new Error(command.error || 'Worker 设备操作失败');
        }
    }
    throw new Error('Worker 设备操作超时');
}

function aoaSetStatus(text, type = '') {
    const node = aoaEl('aoa-run-status');
    if (!node) return;
    node.textContent = text || '待机';
    node.dataset.type = type;
}

function aoaSetBusy(running, text = '') {
    aoaPanelState.running = running;
    document.querySelectorAll('#aoa-panel [data-aoa-run], #aoa-panel [data-aoa-screen-action]')
        .forEach(node => { node.disabled = running; });
    if (text) aoaSetStatus(text, running ? 'running' : '');
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

function aoaReplaceOptions(select, entries, emptyLabel) {
    if (!select) return;
    const previous = select.value;
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
        option.textContent = entry.label;
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

function aoaFillDeviceSelect(selectId, workerId, recorder = false) {
    const devices = aoaUsableDevices(workerId, recorder);
    aoaReplaceOptions(aoaEl(selectId), devices.map(device => ({
        value: aoaDeviceSerial(device), label: aoaDeviceLabel(device),
    })), recorder ? '（无 ADB 可用录制设备）' : '（该 Worker 无本地 USB 设备）');
}

function aoaSyncDeviceOptions() {
    aoaFillDeviceSelect('aoa-record-device', aoaEl('aoa-record-worker')?.value || '', true);
    aoaFillDeviceSelect('aoa-replay-device', aoaEl('aoa-replay-worker')?.value || '', false);
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
    aoaReplaceOptions(aoaEl('aoa-replay-worker'), entries, '（无可用 Worker）');
    aoaSyncDeviceOptions();

}

function aoaRecorderWorkerChanged() {
    aoaFillDeviceSelect('aoa-record-device', aoaEl('aoa-record-worker')?.value || '', true);
    aoaRecorderDeviceChanged();
}

function aoaReplayWorkerChanged() {
    aoaFillDeviceSelect('aoa-replay-device', aoaEl('aoa-replay-worker')?.value || '', false);
}

function aoaRecorderDeviceChanged() {
    aoaPanelState.calibration = null;
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
        const status = aoaEl('aoa-screen-status');
        if (status) status.textContent = '设备已切换，请刷新投屏';
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
    const profile = aoaSelectedProfile();
    if (!profile) { showToast('请先选择一个 AOA 配置', 'warning'); return; }
    aoaPanelState.commands = [...(profile.commands || [])];
    aoaPanelState.editingProfileId = profile.id;
    aoaEl('aoa-profile-name').value = profile.name;
    aoaEl('aoa-replay-wait').value = profile.wait_adb_seconds || 60;
    aoaRenderCommands();
    aoaSetStatus(`已载入：${profile.name}`);
}

function aoaRenderCommands() {
    const list = aoaEl('aoa-command-list');
    const count = aoaEl('aoa-command-count');
    if (count) count.textContent = String(aoaPanelState.commands.length);
    if (!list) return;
    list.replaceChildren();
    if (!aoaPanelState.commands.length) {
        const empty = document.createElement('li');
        empty.className = 'aoa-command-empty';
        empty.textContent = '开始录制后，在左侧操作设备 UI';
        list.append(empty);
        return;
    }
    aoaPanelState.commands.forEach((command, index) => {
        const row = document.createElement('li');
        const content = document.createElement('div');
        content.className = 'aoa-command-row';
        const code = document.createElement('code');
        code.textContent = command;
        const remove = document.createElement('button');
        remove.className = 'btn-xxs'; remove.type = 'button'; remove.textContent = '删除';
        remove.addEventListener('click', () => {
            aoaPanelState.commands.splice(index, 1);
            aoaPanelState.editingProfileId = '';
            aoaRenderCommands();
        });
        content.append(code, remove); row.append(content); list.append(row);
    });
    list.scrollTop = list.scrollHeight;
}

function aoaAppendCommand(command, addPace = true) {
    if (!command) return;
    const additions = [command];
    if (addPace && !command.startsWith('sleep ')) additions.push('sleep 800');
    if (aoaPanelState.commands.length + additions.length > 64) {
        aoaRecordingStop();
        throw new Error('AOA 命令已达到 64 条上限，请保存当前配置');
    }
    aoaPanelState.commands.push(...additions);
    aoaPanelState.editingProfileId = '';
    aoaRenderCommands();
}

async function aoaRecordingStart() {
    if (aoaPanelState.commands.length) {
        const ok = await showConfirmDialog('开始新录制', '当前命令会被清空，是否继续？');
        if (!ok) return;
    }
    if (!aoaEl('aoa-record-worker')?.value || !aoaEl('aoa-record-device')?.value) {
        showToast('请选择 ADB 可用的录制 Worker 和设备', 'warning'); return;
    }
    aoaPanelState.commands = []; aoaPanelState.editingProfileId = '';
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
    if (!aoaPanelState.commands.length) return;
    if (aoaPanelState.commands.at(-1)?.startsWith('sleep ')) aoaPanelState.commands.pop();
    if (aoaPanelState.commands.length) aoaPanelState.commands.pop();
    aoaRenderCommands();
}

async function aoaCommandsClear() {
    if (!aoaPanelState.commands.length) return;
    const ok = await showConfirmDialog('清空 AOA 命令', '确定清空当前命令轨道？');
    if (!ok) return;
    aoaPanelState.commands = []; aoaPanelState.editingProfileId = '';
    aoaRenderCommands();
}

function aoaCommandAdd() {
    const input = aoaEl('aoa-command-input');
    const command = input?.value.trim();
    if (!command) return;
    try { aoaAppendCommand(command, false); input.value = ''; }
    catch (error) { showToast(error.message, 'warning'); }
}

function aoaCommandsExport() {
    if (!aoaPanelState.commands.length) { showToast('当前没有可导出的命令', 'warning'); return; }
    const payload = {
        version: 1,
        name: aoaEl('aoa-profile-name')?.value.trim() || 'AOA Profile',
        wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || 60,
        commands: aoaPanelState.commands,
    };
    const url = URL.createObjectURL(new Blob(
        [JSON.stringify(payload, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'aoa-profile.json';
    link.click(); URL.revokeObjectURL(url);
}

function aoaCommandsImport() { aoaEl('aoa-command-file')?.click(); }

async function aoaImportFileChanged(event) {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file) return;
    try {
        const parsed = JSON.parse(await file.text());
        const commands = Array.isArray(parsed) ? parsed : parsed.commands;
        if (!Array.isArray(commands) || !commands.length || commands.length > 64
                || commands.some(item => typeof item !== 'string' || !item.trim())) {
            throw new Error('JSON 必须包含 1..64 条非空 commands');
        }
        aoaPanelState.commands = commands.map(item => item.trim());
        aoaPanelState.editingProfileId = '';
        if (!Array.isArray(parsed) && parsed.name) {
            aoaEl('aoa-profile-name').value = String(parsed.name).slice(0, 120);
        }
        if (!Array.isArray(parsed) && parsed.wait_adb_seconds) {
            aoaEl('aoa-replay-wait').value = String(parsed.wait_adb_seconds);
        }
        aoaRenderCommands();
    } catch (error) { showToast(`导入失败：${error.message}`, 'warning'); }
}

function aoaRecorderTarget() {
    const workerId = aoaEl('aoa-record-worker')?.value || '';
    const device = aoaEl('aoa-record-device')?.value || '';
    if (!workerId || !device) throw new Error('请选择录制 Worker 和设备');
    return {workerId, device};
}

async function aoaRefreshScreen() {
    // 实时投屏激活期间画面由流自更新：截图刷新（手动按钮/回退轮询残留）
    // 一律跳过，避免「正在获取…/已连接」状态在实时状态上反复跳动。
    if (window.aoaLiveScreen?.isActive()) return;
    let target;
    try { target = aoaRecorderTarget(); }
    catch (error) { showToast(error.message, 'warning'); return; }
    const refreshId = ++aoaPanelState.screenRefreshId;
    const status = aoaEl('aoa-screen-status');
    if (status) status.textContent = '正在获取设备截图…';
    try {
        const result = await executeClusterCommandAndWait({
            worker_id: target.workerId, devices: [target.device], action: 'screenshot',
        }, () => refreshId === aoaPanelState.screenRefreshId && aoaViewIsOpen());
        if (refreshId !== aoaPanelState.screenRefreshId) return;
        if (!result?.image) throw new Error(result?.error || '截图为空');
        const image = aoaEl('aoa-screen-image'); image.src = result.image; image.hidden = false;
        aoaEl('aoa-screen-empty').hidden = true;
        if (status) status.textContent = `已连接 ${target.device} · 单击、长按或拖动操作`;
    } catch (error) {
        if (refreshId !== aoaPanelState.screenRefreshId) return;
        aoaEl('aoa-screen-image').hidden = true;
        const empty = aoaEl('aoa-screen-empty'); empty.hidden = false;
        empty.textContent = '无法获取画面。AOA 回放或 USB 调试关闭后，ADB 截图会暂时不可用。';
        if (status) status.textContent = `截图失败：${error.message}`;
    }
}

async function aoaExecuteRecorderInput(input, {refresh = true, pace = true} = {}) {
    let target;
    try { target = aoaRecorderTarget(); }
    catch (error) { showToast(error.message, 'warning'); return null; }
    const payload = {...input};
    if (aoaPanelState.calibration?.measured) {
        payload.scale_x = aoaPanelState.calibration.scale_x;
        payload.scale_y = aoaPanelState.calibration.scale_y;
    }
    aoaSetBusy(true, `执行 ${input.action}…`);
    try {
        let result = await aoaApi('/api/devices/aoa/record/input', {
            method: 'POST', body: {worker_id: target.workerId, device: target.device, input: payload},
        });
        result = await aoaAwaitCommand(result);
        if (result?.success === false) throw new Error(result.error || `${input.action} 执行失败`);
        if (aoaPanelState.recording && result?.command) aoaAppendCommand(result.command, pace);
        if (result?.touch_calibration) {
            aoaPanelState.calibration = result.touch_calibration; aoaRenderCalibration();
        }
        aoaSetStatus(aoaPanelState.recording ? '录制中' : '操作完成', 'success');
        // 实时投屏激活时画面自更新，跳过截图刷新（省一条命令+轮询）。
        if (refresh && !aoaLiveInputSurfaceActive()) {
            setTimeout(() => aoaRefreshScreen(), 500);
        }
        return result;
    } catch (error) {
        aoaSetStatus(`操作失败：${error.message}`, 'error');
        showToast(`AOA 录制操作失败：${error.message}`, 'warning');
        return null;
    } finally { aoaSetBusy(false); }
}

function aoaLiveTarget() {
    const workerId = aoaEl('aoa-record-worker')?.value || '';
    const device = aoaEl('aoa-record-device')?.value || '';
    if (!workerId || !device) return null;
    return {workerId, device};
}

async function aoaToggleLiveScreen() {
    if (window.aoaLiveScreen?.isActive()) {
        window.aoaLiveScreen.stop();
        aoaEl('aoa-toggle-live')?.setAttribute('data-live', 'off');
        aoaSetStatus('实时投屏已关闭');
        return;
    }
    const target = aoaLiveTarget();
    if (!target) { showToast('请选择录制 Worker 和设备', 'warning'); return; }
    await window.aoaLiveScreen.start(target.device, target.workerId);
    const on = window.aoaLiveScreen.isActive();
    aoaEl('aoa-toggle-live')?.setAttribute('data-live', on ? 'on' : 'off');
}

function aoaLiveInputSurfaceActive() {
    return Boolean(window.aoaLiveScreen?.isActive());
}

function aoaSendKey(key) { return aoaExecuteRecorderInput({action: key}); }

function aoaSendText() {
    const node = aoaEl('aoa-input-text'); const text = node?.value || '';
    if (!text) return;
    return aoaExecuteRecorderInput({action: 'write', text}).then(result => {
        if (result) node.value = '';
    });
}

async function aoaCalibrateTouch() {
    const ok = await showConfirmDialog(
        'AOA 触摸标定',
        '标定会把录制机短暂切入 AOAv2 模式并点击屏幕中心。请先停在点击中心无副作用的页面，是否继续？');
    if (!ok) return;
    const result = await aoaExecuteRecorderInput({action: 'calibrate'}, {refresh: true, pace: false});
    if (result?.touch_calibration?.measured) showToast('AOA 触摸映射标定成功', 'success');
}

function aoaRenderCalibration() {
    const node = aoaEl('aoa-calibration-status'); if (!node) return;
    const calibration = aoaPanelState.calibration;
    node.textContent = calibration?.measured
        ? `触摸映射：已实测（X ${calibration.scale_x} / Y ${calibration.scale_y} 像素每逻辑单位）`
        : '触摸映射：未标定，使用显示分辨率按轴换算';
}

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
    const name = aoaEl('aoa-profile-name')?.value.trim();
    if (!name || !aoaPanelState.commands.length) {
        showToast('请填写配置名称，并至少录制一条命令', 'warning'); return;
    }
    const body = {name, commands: aoaPanelState.commands,
        wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || 60,
        description: `Web AOA interactive recording from ${aoaEl('aoa-record-device')?.value || 'device'}`};
    try {
        const path = aoaPanelState.editingProfileId
            ? `/api/devices/aoa/profiles/${encodeURIComponent(aoaPanelState.editingProfileId)}`
            : '/api/devices/aoa/profiles/from-recording';
        const result = await aoaApi(path, {
            method: aoaPanelState.editingProfileId ? 'PUT' : 'POST', body,
        });
        aoaPanelState.editingProfileId = result.profile.id;
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
    const worker = aoaEl('aoa-replay-worker')?.value;
    const device = aoaEl('aoa-replay-device')?.value;
    const profile = profileId ? aoaPanelState.profiles.find(item => item.id === profileId) : aoaSelectedProfile();
    if (!worker || !device || !profile) { showToast('请选择目标 Worker、设备和 AOA 配置', 'warning'); return; }
    const ok = await showConfirmDialog('AOA 回放',
        `将在 ${device} 上通过 USB 回放“${profile.name}”（${profile.commands.length} 条），期间设备会短暂断开 ADB。继续？`);
    if (!ok) return;
    aoaSetBusy(true, `回放中：${profile.name}`);
    try {
        let result = await aoaApi('/api/devices/aoa/recover', {method: 'POST', body: {
            worker_id: worker, device, profile_id: profile.id,
            wait_adb_seconds: Number(aoaEl('aoa-replay-wait')?.value) || profile.wait_adb_seconds || 60,
        }});
        result = await aoaAwaitCommand(result);
        aoaShowResult(`回放完成：${profile.name}`, result);
        aoaSetStatus(result?.success === false ? '回放未完成' : '回放完成',
            result?.success === false ? 'error' : 'success');
    } catch (error) {
        aoaShowResult(`回放失败：${profile.name}`, {error: error.message});
        aoaSetStatus(`回放失败：${error.message}`, 'error');
    } finally { aoaSetBusy(false); }
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
        // 清单同步只更新选择器；投屏由用户操作启动。
        if (typeof loadDevicesManagement === 'function') await loadDevicesManagement();
        if (typeof stopDevicesAutoRefresh === 'function') stopDevicesAutoRefresh();
    } else {
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
}

// 兼容初版 AOA 页签导出的调用名。
function aoaRecordStart() { return aoaRecordingStart(); }
function aoaProfileReplay(profileId) { return aoaReplaySelected(profileId); }
function aoaProfileDelete() { return aoaProfileDeleteSelected(); }

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', aoaInitializeWorkbench, {once: true});
} else { aoaInitializeWorkbench(); }
